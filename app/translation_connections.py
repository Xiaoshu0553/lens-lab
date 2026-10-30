"""Private multi-provider configuration and actual JSON API calls for this study.

Connection keys stay in a mode-0600 local file, never in sessions, exports or API
responses. Public configuration and successful HTTP probes are distinct states.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
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
from .translation_panel_quality import (MODEL_MIN, MODEL_MAX, DISTINCT_MODE, REPEATED_MODE,
    EVALUATION_MODES, validate_panel)


_LOCAL = {'localhost','127.0.0.1','::1'}
_JSON_FORMAT_REMINDER = (
    '请继续遵守系统已有的 JSON 结构。用户对正文格式的要求只约束相应 JSON 字符串字段；'
    '顶层仍须输出完整 JSON 对象，不要输出空白。不要添加、删改既有结构要求的字段。'
)


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
        self._last_response_model=ContextVar(f'last_response_model_{id(self)}',default=None)
        self._response_metadata=ContextVar(f'response_metadata_{id(self)}',default=None)
        self._format_retry_count=ContextVar(f'json_format_retry_count_{id(self)}',default=0)

    @property
    def last_response_model(self):
        return self._last_response_model.get()

    @last_response_model.setter
    def last_response_model(self,value):
        self._last_response_model.set(value)

    @property
    def response_metadata(self):
        """Non-secret final-response provenance, scoped to the current task."""
        return deepcopy(self._response_metadata.get() or {})

    @property
    def last_response_metadata(self):
        """Compatibility name for the current task's last response metadata."""
        return self.response_metadata

    @property
    def format_retry_count(self):
        """Diagnostic for the most recent call in this async task's context."""
        return self._format_retry_count.get()

    async def complete(self,messages,*,temperature=None):
        self._format_retry_count.set(0)
        self._response_metadata.set({})
        self.last_response_model=None
        cfg=self.connection
        endpoint=urlsplit(cfg['base_url'])
        host=endpoint.hostname
        native_ollama=host in _LOCAL and endpoint.port==11434
        original_messages=deepcopy(messages)
        payload=dict(model=cfg['model'],messages=original_messages,temperature=cfg['temperature'] if temperature is None else temperature,
                     max_tokens=cfg['max_tokens'],stream=False)
        if cfg['json_mode']:
            payload['response_format']={'type':'json_object'}
        if native_ollama:
            payload=dict(model=cfg['model'],messages=original_messages,stream=False,
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
                    # Both format attempts and transport retries share this one
                    # 120-second deadline. A format recovery never gets a new budget.
                    for format_attempt in range(2):
                        # Preserve the original transport retry, but send the
                        # additional protocol reminder at most once in total.
                        for attempt in range(2 if not format_attempt else 1):
                            try:
                                request_url=(f'{endpoint.scheme}://{endpoint.netloc}/api/chat' if native_ollama
                                             else cfg['base_url']+'/chat/completions')
                                response=await client.post(request_url,json=payload,headers=headers)
                                self._response_metadata.set({'http_status':response.status_code})
                                response.raise_for_status()
                                break
                            except httpx.HTTPError as exc:
                                status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
                                if format_attempt or attempt or not (isinstance(exc,httpx.TransportError) or status in {429,500,502,503,504}):
                                    raise
                                await asyncio.sleep(1)
                        body=response.json()
                        if native_ollama:
                            if not isinstance(body,dict) or not body.get('done') or body.get('done_reason')!='stop':
                                raise ModelError('本地模型未正常结束或输出达到上限','output_interrupted')
                            message=body.get('message')
                            normal_stop=True
                        elif not isinstance(body,dict) or not isinstance(body.get('choices'),list) or not body['choices']:
                            raise ModelError('模型返回结构无效','invalid_response')
                        else:
                            choice=body['choices'][0]
                            if not isinstance(choice,dict) or choice.get('finish_reason') not in (None,'stop'):
                                raise ModelError('模型输出被截断或未正常结束，请调整输出上限后重试','output_interrupted')
                            message=choice.get('message')
                            normal_stop=choice.get('finish_reason')=='stop'
                        if not isinstance(message,dict) or not isinstance(message.get('content'),str) or not message['content'].strip():
                            # DeepSeek documents occasional empty JSON-mode
                            # content. Ask for the existing protocol once; never
                            # substitute reasoning text or loosen application JSON validation.
                            if (not format_attempt and cfg['json_mode'] and normal_stop
                                    and isinstance(message,dict) and message.get('refusal') is None
                                    and isinstance(message.get('content'),str) and not message['content'].strip()):
                                self._format_retry_count.set(1)
                                payload={**payload,'messages':original_messages+[
                                    {'role':'system','content':_JSON_FORMAT_REMINDER}]}
                                continue
                            raise ModelError('模型未返回最终正文','empty_content')
                        content=message['content'].strip()
                        if len(content)>50000:
                            raise ModelError('模型输出超过允许长度','output_too_long')
                        reported=body.get('model')
                        self.last_response_model=(reported if isinstance(reported,str)
                            and re.fullmatch(r'[A-Za-z0-9._:/@+\-]{1,240}',reported)
                            and not (cfg.get('api_key') and cfg['api_key'] in reported) else None)
                        response_id=body.get('id')
                        safe_id=(response_id if isinstance(response_id,str)
                            and re.fullmatch(r'[A-Za-z0-9._:/@+\-]{1,240}',response_id)
                            and not (cfg.get('api_key') and cfg['api_key'] in response_id) else None)
                        raw_usage=body.get('usage')
                        usage={key:value for key,value in (raw_usage.items() if isinstance(raw_usage,dict) else [])
                            if key in {'prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens'}
                            and type(value) is int and 0 <= value <= 1_000_000_000}
                        if native_ollama:
                            for raw_key,key in (('prompt_eval_count','input_tokens'),('eval_count','output_tokens')):
                                value=body.get(raw_key)
                                if type(value) is int and 0 <= value <= 1_000_000_000:
                                    usage[key]=value
                        self._response_metadata.set(dict(id=safe_id,model=self.last_response_model,usage=usage,
                            finish_reason=body.get('done_reason') if native_ollama else choice.get('finish_reason'),
                            content_sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),
                            http_status=response.status_code,json_format_retries=self.format_retry_count))
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
        self.config=dict(dialogue=default,judges=[],evaluation_mode=DISTINCT_MODE,evaluation_repetitions=5)
        if self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            self.config=self._validated_update(raw,self.config)

    def _validated_update(self,body,previous):
        if not isinstance(body,dict) or set(body)-{'dialogue','judges','version','evaluation_mode','evaluation_repetitions'}:
            raise ValueError('模型配置字段无效')
        config=deepcopy(previous)
        mode=body.get('evaluation_mode',previous.get('evaluation_mode',DISTINCT_MODE))
        repetitions=body.get('evaluation_repetitions',previous.get('evaluation_repetitions',5))
        if not isinstance(mode,str) or mode not in EVALUATION_MODES:
            raise ValueError('评审模式须为distinct_models或repeated_sessions')
        if type(repetitions) is not int or not MODEL_MIN <= repetitions <= MODEL_MAX:
            raise ValueError(f'重复盲评会话数须为{MODEL_MIN}至{MODEL_MAX}之间的整数')
        config.update(evaluation_mode=mode,evaluation_repetitions=repetitions)
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

    @staticmethod
    def _evaluation_connections(config):
        if config.get('evaluation_mode',DISTINCT_MODE)==REPEATED_MODE:
            # Only server-side copies access the existing dialogue credential.
            # Every entry is a new stateless request/context, not a new model.
            return [dict(deepcopy(config['dialogue']),id=f'session-{index+1}',
                         label=f'评审会话{index+1}',weight=1.0)
                    for index in range(config.get('evaluation_repetitions',5))]
        return deepcopy(config.get('judges',[]))

    def evaluation_connections(self):
        """Private effective panel; credentials never enter public snapshots."""
        with self.lock:
            return self._evaluation_connections(self.config)

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
            mode=config.get('evaluation_mode',DISTINCT_MODE)
            effective=self._evaluation_connections(config)
            try:
                validate_panel(effective,mode=mode)
                missing=[c['label'] for c in effective if urlsplit(c['base_url']).hostname not in _LOCAL and not c.get('api_key')]
                if missing:
                    raise ValueError('这些远程模型尚未填写密钥：'+'、'.join(missing))
                ready=True;error=None
            except ValueError as exc:
                ready=False;error=str(exc)
            return dict(dialogue=public(config['dialogue']),judges=[public(c) for c in effective],
                configured_judges=[public(c) for c in config['judges']],
                evaluation_mode=mode,evaluation_repetitions=config.get('evaluation_repetitions',5),
                panel=dict(ready=ready,minimum=MODEL_MIN,maximum=MODEL_MAX,error=error,mode=mode,count=len(effective),
                           independent_model_count=1 if mode==REPEATED_MODE else len(effective)),
                model_api_verified=bool(self.verifications.get(self.identity(config['dialogue'])))
                    and self.identity(config['dialogue']) not in self.errors)

    async def check(self,target='all'):
        config=self.connections()
        selected=([config['dialogue']] if target in {'dialogue','all'} else [])+(self._evaluation_connections(config) if target in {'judges','all'} else [])
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
                            response_metadata=provider.response_metadata,
                            verified_at=self.verifications[self.identity(connection)],seconds=round(time.monotonic()-started,3))
            except Exception as exc:
                code=exc.code if isinstance(exc,ModelError) else 'invalid_probe'
                detail=code+(f' HTTP {exc.http_status}' if isinstance(exc,ModelError) and exc.http_status else '')
                self.mark_error(connection,detail)
                return dict(id=connection['id'],model=connection['model'],ok=False,error=detail,seconds=round(time.monotonic()-started,3))
        checks=await asyncio.gather(*(one(c) for c in selected))
        return dict(**self.snapshot(),checks=checks)
