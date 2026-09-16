"use strict";

(() => {
  const tableBody = document.querySelector("#device-table-body");
  const total = document.querySelector("#device-total");
  const message = document.querySelector("#management-message");
  const refreshButton = document.querySelector("#refresh-devices");
  const logoutButton = document.querySelector("#logout-button");
  const openTrackMap = document.querySelector("#open-track-map");

  const beijingTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  function showMessage(text, type = "info") {
    message.textContent = text;
    message.className = `message${type === "info" ? "" : ` ${type}`}`;
  }

  function redirectToLogin() {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.replace(`/login.html?next=${encodeURIComponent(next)}`);
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    if (response.status === 401) {
      redirectToLogin();
      throw new Error("登录状态已失效。");
    }
    if (!response.ok) {
      throw new Error(`请求失败（HTTP ${response.status}）。`);
    }
    return response.status === 204 ? null : response.json();
  }

  function formatTime(value) {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime())
      ? `${beijingTimeFormatter.format(date)} UTC+8`
      : "—";
  }

  function formatPosition(device) {
    const latitude = Number(device.latitude);
    const longitude = Number(device.longitude);
    if (device.latitude === null || device.longitude === null
      || !Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      return "—";
    }
    return `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;
  }

  function statusBadge(status) {
    const values = {
      online: ["在线", "status-online"],
      warning: ["连接可能中断", "status-warning"],
      offline: ["离线", "status-offline"],
    };
    const [label, className] = values[status] || ["未知", "status-unknown"];
    const badge = document.createElement("span");
    badge.className = `status-badge ${className}`;
    badge.textContent = label;
    return badge;
  }

  function textCell(value) {
    const cell = document.createElement("td");
    cell.textContent = value;
    return cell;
  }

  function renderDevices(devices) {
    tableBody.replaceChildren();
    total.textContent = `${devices.length} 台`;
    for (const device of devices) {
      const row = document.createElement("tr");
      row.append(textCell(device.imei || "—"));

      const nameCell = document.createElement("td");
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "device-name-input";
      nameInput.maxLength = 64;
      nameInput.value = device.name || "";
      nameInput.placeholder = "未命名";
      nameInput.setAttribute("aria-label", `${device.imei} 的设备别名`);
      nameCell.append(nameInput);
      row.append(nameCell);

      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(device.online_status));
      row.append(statusCell);
      row.append(textCell(formatTime(device.last_seen)));
      row.append(textCell(formatPosition(device)));
      row.append(textCell(
        `${device.satellites ?? "—"} / ${device.hdop ?? "—"} / ${device.csq ?? "—"}`,
      ));

      const actionCell = document.createElement("td");
      const saveButton = document.createElement("button");
      saveButton.type = "button";
      saveButton.className = "primary-button compact-button";
      saveButton.textContent = "保存别名";
      saveButton.addEventListener("click", () => saveDeviceName(device.imei, nameInput, saveButton));
      actionCell.append(saveButton);
      row.append(actionCell);
      tableBody.append(row);
    }
  }

  async function loadDevices() {
    refreshButton.disabled = true;
    showMessage("正在加载设备…");
    try {
      const devices = await fetchJson("/api/devices");
      if (!Array.isArray(devices)) {
        throw new Error("设备列表格式无效。");
      }
      renderDevices(devices);
      showMessage(devices.length ? `已加载 ${devices.length} 台设备。` : "当前没有设备。", "success");
    } catch (error) {
      tableBody.replaceChildren();
      total.textContent = "0 台";
      showMessage(error.message || "设备加载失败。", "error");
    } finally {
      refreshButton.disabled = false;
    }
  }

  async function saveDeviceName(imei, input, button) {
    button.disabled = true;
    try {
      const updated = await fetchJson(`/api/devices/${encodeURIComponent(imei)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: input.value.trim() || null }),
      });
      input.value = updated.name || "";
      showMessage(`设备 ${imei} 的别名已保存。`, "success");
    } catch (error) {
      showMessage(error.message || "设备别名保存失败。", "error");
    } finally {
      button.disabled = false;
    }
  }

  async function logout() {
    logoutButton.disabled = true;
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      window.location.replace("/login.html");
    }
  }

  refreshButton.addEventListener("click", loadDevices);
  openTrackMap.addEventListener("click", () => {
    window.location.href = "/";
  });
  logoutButton.addEventListener("click", logout);
  loadDevices();
})();
