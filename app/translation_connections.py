"""Private multi-provider configuration and actual JSON API calls for this study.

Connection keys stay in a mode-0600 local file, never in sessions, exports or API
responses. Public configuration and successful HTTP probes are distinct states.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx

from .db import now_utc
from .model import ModelError
from .translation_panel_quality import MODEL_MIN, MODEL_MAX, validate_panel


_LOCAL = {'localhost','127.0.0.1','::1'}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def public_connection(value):
    return {key: value[key] for key in ('id','label','model','base_url','weight','temperature','max_tokens','json_mode','thinking') if key in value}


def clean_connection(value, previous=None, *, dialogue=False):
    if not isinstance(value,dict):
        raise ValueError('模型连接必须是对象')
    previous=previous or {}
    model=value.get('model','').strip() if isinstance(value.get('model'),str) else ''
    base=value.get('base_url','').strip().rstrip('/') if isinstance(value.get('base_url'),str) else ''
    if not model or len(model)>180:
        raise ValueError('请填写实际模型名称')
    try:
        url=urlsplit(base)
        url.port
    except ValueError as exc:
        raise ValueError('模型地址的主机或端口无效') from exc
    if (url.scheme not in {'http','https'} or not url.hostname or url.username or url.password
            or url.query or url.fragment or len(base)>500 or any(char.isspace() for char in base)):
        raise ValueError('模型地址须为不带密钥、用户名或查询参数的HTTP(S) API根地址')
    if url.scheme=='http' and url.hostname not in _LOCAL:
        raise ValueError('远程模型连接必须使用HTTPS；本机Ollama可以使用HTTP')
    ident='dialogue' if dialogue else value.get('id','')
    if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,49}',ident):
        raise ValueError('评审模型编号须以字母开头，仅含字母、数字、下划线或短横线')
    label=value.get('label',model)
    if not isinstance(label,str) or not label.strip() or len(label)>120:
        raise ValueError('请填写模型显示名称')
    weight=value.get('weight',1)
    temperature=value.get('temperature',0)
    tokens=value.get('max_tokens',4096 if not dialogue else 2048)
    if type(weight) not in (int,float) or not math.isfinite(weight) or not 0<weight<=1000:
        raise ValueError('模型权重须为大于0且不超过1000的有限数值')
    if type(temperature) not in (int,float) or not math.isfinite(temperature) or not 0<=temperature<=2:
        raise ValueError('温度须在0至2之间')
    if type(tokens) is not int or not 128<=tokens<=16384:
        raise ValueError('输出上限须为128至16384的整数')
    json_mode=value.get('json_mode',True)
    thinking=value.get('thinking','disabled')
    if type(json_mode) is not bool or thinking not in ('disabled','enabled'):
        raise ValueError('JSON模式或思考模式配置无效')
    supplied=value.get('api_key','')
    if not isinstance(supplied,str) or len(supplied)>2000:
        raise ValueError('API密钥格式无效')
    supplied=supplied.strip()
    # An empty password field preserves a key only for the same endpoint/id.
    key=supplied or (previous.get('api_key','') if previous.get('base_url')==base and previous.get('id')==ident else '')
    if value.get('clear_api_key') is True:
        key=''
    if key in {'自行填写API','YOUR_API_KEY','请自行填写API'}:
        key=''
    return dict(id=ident,label=label.strip(),model=model,base_url=base,weight=float(weight),
                temperature=float(temperature),max_tokens=tokens,json_mode=json_mode,thinking=thinking,api_key=key)


class JSONModelProvider:
    """OpenAI-compatible chat API. Each instance belongs to one actual model."""
    def __init__(self, connection):
        self.connection=deepcopy(connection)
        self.last_response_model=None

    async def complete(self,messages,*,temperature=None):
        cfg=self.connection
        endpoint=urlsplit(cfg['base_url'])
        host=endpoint.hostname
        native_ollama=host in _LOCAL and endpoint.port==11434
        payload=dict(model=cfg['model'],messages=messages,temperature=cfg['temperature'] if temperature is None else temperature,
                     max_tokens=cfg['max_tokens'],stream=False)
        if cfg['json_mode']:
            payload['response_format']={'type':'json_object'}
        if native_ollama:
            payload=dict(model=cfg['model'],messages=messages,stream=False,
                         think=cfg['thinking']=='enabled',
                         options=dict(temperature=cfg['temperature'] if temperature is None else temperature,
                                      num_predict=cfg['max_tokens']))
            if cfg['json_mode']:
                payload['format']='json'
        elif host in _LOCAL:
            payload['think']=cfg['thinking']=='enabled'
        elif host=='api.deepseek.com':
            payload['thinking']={'type':cfg['thinking']}
        headers={'Authorization':'Bearer '+cfg['api_key']} if cfg.get('api_key') else {}
        try:
            async with asyncio.timeout(120):
                # Loopback must bypass system HTTP proxies. Remote calls retain
                # the user's configured network proxy.
                async with httpx.AsyncClient(timeout=120,trust_env=host not in _LOCAL,follow_redirects=False) as client:
                    for attempt in range(2):
                        try:
                            request_url=(f'{endpoint.scheme}://{endpoint.netloc}/api/chat' if native_ollama
                                         else cfg['base_url']+'/chat/completions')
                            response=await client.post(request_url,json=payload,headers=headers)
                            response.raise_for_status()
                            break
                        except httpx.HTTPError as exc:
                            status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
                            if attempt or not (isinstance(exc,httpx.TransportError) or status in {429,500,502,503,504}):
                                raise
                            await asyncio.sleep(1)
                    body=response.json()
            if native_ollama:
                if not isinstance(body,dict) or not body.get('done') or body.get('done_reason')!='stop':
                    raise ModelError('本地模型未正常结束或输出达到上限','output_interrupted')
                message=body.get('message')
            elif not isinstance(body,dict) or not isinstance(body.get('choices'),list) or not body['choices']:
                raise ModelError('模型返回结构无效','invalid_response')
            else:
                choice=body['choices'][0]
                if not isinstance(choice,dict) or choice.get('finish_reason') not in (None,'stop'):
                    raise ModelError('模型输出被截断或未正常结束，请调整输出上限后重试','output_interrupted')
                message=choice.get('message')
            if not isinstance(message,dict) or not isinstance(message.get('content'),str) or not message['content'].strip():
                raise ModelError('模型未返回最终正文','empty_content')
            content=message['content'].strip()
            if len(content)>50000:
                raise ModelError('模型输出超过允许长度','output_too_long')
            reported=body.get('model')
            self.last_response_model=(reported if isinstance(reported,str)
                and re.fullmatch(r'[A-Za-z0-9._:/@+\-]{1,240}',reported)
                and not (cfg.get('api_key') and cfg['api_key'] in reported) else None)
            return content
        except (httpx.HTTPError,TimeoutError,ValueError,TypeError,KeyError) as exc:
            status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
            code='provider_timeout' if isinstance(exc,(TimeoutError,httpx.TimeoutException)) else 'provider_http_error' if isinstance(exc,httpx.HTTPError) else 'invalid_response'
            raise ModelError('模型API调用失败：'+type(exc).__name__,code,http_status=status) from exc


class ConnectionRegistry:
    def __init__(self,settings):
        self.path=settings.translation_models_path or settings.database_path.parent/'.translation-models.json'
        self.lock=threading.RLock()
        self.verifications={}
        self.reported_models={}
        self.errors={}
        try:
            default=clean_connection(dict(id='dialogue',label=settings.model_name,model=settings.model_name,
                base_url=settings.model_base_url,api_key=settings.model_api_key,max_tokens=max(128,settings.model_max_tokens),
                temperature=settings.model_temperature,thinking=settings.model_thinking),dialogue=True)
        except ValueError:
            if settings.study_mode!='demo':
                raise
            # Historical rule-only showcases used non-URL labels such as DEMO.
            # They never contact a provider, and must not imply a verified API.
            default=clean_connection(dict(model='not-configured',label='演示未配置真实模型',
                base_url='https://unconfigured.invalid/v1'),dialogue=True)
        self.config=dict(dialogue=default,judges=[])
        if self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            self.config=self._validated_update(raw,self.config)

    def _validated_update(self,body,previous):
        if not isinstance(body,dict) or set(body)-{'dialogue','judges','version'}:
            raise ValueError('模型配置字段无效')
        config=deepcopy(previous)
        if 'dialogue' in body:
            config['dialogue']=clean_connection(body['dialogue'],previous.get('dialogue'),dialogue=True)
        if 'judges' in body:
            if not isinstance(body['judges'],list) or len(body['judges'])>MODEL_MAX:
                raise ValueError(f'评审模型最多{MODEL_MAX}个')
            old={c['id']:c for c in previous.get('judges',[])}
            judges=[clean_connection(item,old.get(item.get('id')) if isinstance(item,dict) else None) for item in body['judges']]
            # Drafts may contain fewer than five, but duplicates are never accepted.
            if len({c['id'] for c in judges})!=len(judges):
                raise ValueError('评审模型编号重复')
            identities=[(c['base_url'].lower(),c['model'].casefold()) for c in judges]
            if len(set(identities))!=len(identities):
                raise ValueError('同一模型连接不能重复作为多个AI')
            if len(judges)>=MODEL_MIN:
                validate_panel(judges)
            config['judges']=judges
        return config

    def update(self,body):
        with self.lock:
            updated=self._validated_update(body,self.config)
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,tmp=tempfile.mkstemp(prefix='.translation-models-',dir=str(self.path.parent))
            try:
                os.fchmod(fd,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as stream:
                    json.dump(dict(version=1,**updated),stream,ensure_ascii=False,indent=2,allow_nan=False)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(tmp,self.path)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            self.config=updated
            return self.snapshot()

    def connections(self):
        with self.lock:
            return deepcopy(self.config)

    def identity(self,connection):
        # Secret fingerprints stay in process memory only; never serialized.
        return _fingerprint(connection)

    def mark_success(self,connection,response_model=None):
        with self.lock:
            self.verifications[self.identity(connection)]=now_utc()
            if response_model:
                self.reported_models[self.identity(connection)]=response_model
            self.errors.pop(self.identity(connection),None)

    def mark_error(self,connection,error):
        with self.lock:
            self.errors[self.identity(connection)]=error

    def snapshot(self):
        with self.lock:
            config=deepcopy(self.config)
            def public(c):
                ident=self.identity(c)
                return dict(**public_connection(c),api_key_configured=bool(c.get('api_key')),
                            verified_at=self.verifications.get(ident),reported_model=self.reported_models.get(ident),
                            last_error=self.errors.get(ident))
            try:
                validate_panel(config['judges'])
                missing=[c['label'] for c in config['judges'] if urlsplit(c['base_url']).hostname not in _LOCAL and not c.get('api_key')]
                if missing:
                    raise ValueError('这些远程模型尚未填写密钥：'+'、'.join(missing))
                ready=True;error=None
            except ValueError as exc:
                ready=False;error=str(exc)
            return dict(dialogue=public(config['dialogue']),judges=[public(c) for c in config['judges']],
                panel=dict(ready=ready,minimum=MODEL_MIN,maximum=MODEL_MAX,error=error),
                model_api_verified=bool(self.verifications.get(self.identity(config['dialogue'])))
                    and self.identity(config['dialogue']) not in self.errors)

    async def check(self,target='all'):
        config=self.connections()
        selected=([config['dialogue']] if target in {'dialogue','all'} else [])+(config['judges'] if target in {'judges','all'} else [])
        async def one(connection):
            started=time.monotonic()
            try:
                provider=JSONModelProvider(connection)
                raw=await provider.complete([
                    {'role':'system','content':'这是模型API连通检查。只输出一个JSON对象，不输出解释。'},
                    {'role':'user','content':'请输出 {"ok":true}。'}],temperature=0)
                result=json.loads(raw)
                if not isinstance(result,dict) or result.get('ok') is not True:
                    raise ModelError('模型回复未通过JSON连通校验','invalid_probe')
                self.mark_success(connection,provider.last_response_model)
                return dict(id=connection['id'],model=connection['model'],ok=True,
                            reported_model=provider.last_response_model,
                            verified_at=self.verifications[self.identity(connection)],seconds=round(time.monotonic()-started,3))
            except Exception as exc:
                code=exc.code if isinstance(exc,ModelError) else 'invalid_probe'
                detail=code+(f' HTTP {exc.http_status}' if isinstance(exc,ModelError) and exc.http_status else '')
                self.mark_error(connection,detail)
                return dict(id=connection['id'],model=connection['model'],ok=False,error=detail,seconds=round(time.monotonic()-started,3))
        checks=await asyncio.gather(*(one(c) for c in selected))
        return dict(**self.snapshot(),checks=checks)
