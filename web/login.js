"use strict";

(() => {
  const form = document.querySelector("#login-form");
  const username = document.querySelector("#username");
  const password = document.querySelector("#password");
  const button = document.querySelector("#login-button");
  const message = document.querySelector("#login-message");

  function showMessage(text, type = "error") {
    message.textContent = text;
    message.className = `message${type === "info" ? "" : ` ${type}`}`;
  }

  function safeDestination() {
    const destination = new URLSearchParams(window.location.search).get("next");
    if (destination && destination.startsWith("/") && !destination.startsWith("//")) {
      return destination;
    }
    return "/";
  }

  async function checkExistingSession() {
    try {
      const response = await fetch("/api/auth/session", {
        headers: { Accept: "application/json" },
      });
      if (response.ok) {
        window.location.replace(safeDestination());
      }
    } catch {
      showMessage("暂时无法连接服务器，请稍后重试。", "error");
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!username.value.trim() || !password.value) {
      showMessage("请输入用户名和密码。", "error");
      return;
    }

    button.disabled = true;
    showMessage("正在登录…", "info");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          username: username.value.trim(),
          password: password.value,
        }),
      });

      if (response.ok) {
        password.value = "";
        window.location.replace(safeDestination());
        return;
      }
      if (response.status === 401) {
        showMessage("用户名或密码错误。", "error");
      } else if (response.status === 429) {
        showMessage("登录尝试过于频繁，请稍后再试。", "error");
      } else {
        showMessage(`登录失败（HTTP ${response.status}）。`, "error");
      }
    } catch {
      showMessage("无法连接服务器，请检查网络后重试。", "error");
    } finally {
      button.disabled = false;
    }
  });

  checkExistingSession();
})();
