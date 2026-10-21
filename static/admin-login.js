const loginForm = document.getElementById("admin-login-form");
const loginButton = document.getElementById("login-button");
const loginError = document.getElementById("login-error");
const loginStatus = document.getElementById("login-status");
const loginParams = new URLSearchParams(window.location.search);
const allowedDestinations = new Set(["/admin", "/admin/preview", "/admin/instruments"]);
const destination = allowedDestinations.has(loginParams.get("next")) ? loginParams.get("next") : "/admin";

if (loginParams.get("expired") === "1") {
  loginStatus.textContent = "登录已过期，请重新登录。";
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (loginButton.disabled) return;
  loginError.textContent = "";
  loginStatus.textContent = "正在登录…";
  loginButton.disabled = true;
  loginButton.textContent = "登录中…";
  try {
    const response = await fetch("/api/admin/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("admin-username").value.trim(),
        password: document.getElementById("admin-password").value
      })
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(typeof body.detail === "string" ? body.detail : `登录失败（${response.status}），请重试。`);
    }
    document.getElementById("admin-password").value = "";
    loginStatus.textContent = "登录成功，正在打开后台…";
    window.location.replace(destination);
  } catch (error) {
    loginStatus.textContent = "";
    loginError.textContent = error instanceof TypeError ? "网络连接失败，请检查网络后重试。" : error.message;
  } finally {
    loginButton.disabled = false;
    loginButton.textContent = "登录";
  }
});
