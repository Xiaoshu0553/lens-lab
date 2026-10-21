/* The independent download catalogue is unnecessary inside a showcase database. */
(() => {
  const container = document.getElementById("synthetic-datasets");
  if (!container) return;
  const allowed = new Set(["calibrated_csv", "calibrated_json", "natural_csv", "pretest_csv", "xlsx", "zip"]);
  let requestVersion = 0;
  let showcaseMode = null;
  function element(tag, text, className = "") {
    const item = document.createElement(tag);
    item.textContent = text;
    item.className = className;
    return item;
  }
  async function loadSyntheticDatasets() {
    if (showcaseMode !== false) return;
    const version = ++requestVersion;
    try {
      const response = await fetch("/api/admin/synthetic-datasets", { credentials: "same-origin", cache: "no-store" });
      if (version !== requestVersion) return;
      if (response.status === 401) {
        const line = element("p", "登录已过期，请 ", "error");
        const login = element("a", "重新登录");
        login.href = "/admin/login?next=%2Fadmin";
        line.append(login);
        container.replaceChildren(line);
        return;
      }
      if (!response.ok) throw new Error("合成数据文件状态暂时无法读取，请点击页面刷新重试。");
      const data = await response.json();
      if (version !== requestVersion) return;
      const sections = [];
      for (const dataset of data.datasets || []) {
        const section = element("div", "");
        if (dataset.available) {
          section.append(element("p", `${dataset.n_total}条合成记录：${dataset.n_completed}条已完成、${dataset.n_dropout}条未完成。导出包含全部年龄区间。`));
        } else {
          section.append(element("p", "合成数据文件尚不可用，请联系研究负责人核对上传文件。", "error"));
        }
        if (dataset.calibration_note) section.append(element("p", dataset.calibration_note, "muted"));
        const actions = element("div", "", "actions");
        for (const file of dataset.files || []) {
          if (!allowed.has(file.key)) continue;
          if (dataset.available && file.available) {
            const link = element("a", file.label, "button-link");
            link.href = `/api/admin/synthetic-export?file=${encodeURIComponent(file.key)}`;
            actions.append(link);
          } else {
            const unavailable = element("span", `${file.label}（暂不可用）`, "muted");
            actions.append(unavailable);
          }
        }
        section.append(actions);
        sections.push(section);
      }
      container.replaceChildren(...(sections.length ? sections : [element("p", "暂无可下载的合成数据。", "muted")]));
    } catch (error) {
      if (version === requestVersion) container.replaceChildren(element("p", error.message, "error"));
    }
  }
  async function initialize() {
    try {
      const response = await fetch("/api/info", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("暂时无法读取环境配置，请刷新页面重试。");
      const info = await response.json();
      showcaseMode = info.showcase_mode === true;
      const section = document.getElementById("synthetic-download-section");
      if (section) section.hidden = showcaseMode;
      if (showcaseMode) return;
      await loadSyntheticDatasets();
    } catch (error) {
      container.replaceChildren(element("p", error.message, "error"));
    }
  }
  document.getElementById("refresh-button")?.addEventListener("click", () =>
    showcaseMode === null ? initialize() : loadSyntheticDatasets());
  initialize();
})();
