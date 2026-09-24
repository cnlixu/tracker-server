"use strict";

(() => {
  const DEFAULT_MAP_CENTER = [30.67, 104.06];
  const TRACK_COLOR = "#176b62";
  const elements = {
    deviceSelect: document.querySelector("#device-select"),
    trackStart: document.querySelector("#track-start"),
    trackEnd: document.querySelector("#track-end"),
    queryTrack: document.querySelector("#query-track"),
    refreshDevices: document.querySelector("#refresh-devices"),
    logoutButton: document.querySelector("#logout-button"),
    openDeviceManagement: document.querySelector("#open-device-management"),
    timeRangeField: document.querySelector("#time-range-field"),
    timeRangeTrigger: document.querySelector("#time-range-trigger"),
    timeRangeDisplay: document.querySelector("#time-range-display"),
    timeRangePopover: document.querySelector("#time-range-popover"),
    timeRangeDone: document.querySelector("#time-range-done"),
    message: document.querySelector("#page-message"),
    onlineStatus: document.querySelector("#online-status"),
    deviceImei: document.querySelector("#device-imei"),
    deviceName: document.querySelector("#device-name"),
    lastSeen: document.querySelector("#last-seen"),
    lastGpsTime: document.querySelector("#last-gps-time"),
    validStatus: document.querySelector("#valid-status"),
    coordinates: document.querySelector("#coordinates"),
    altitude: document.querySelector("#altitude"),
    speed: document.querySelector("#speed"),
    course: document.querySelector("#course"),
    satellites: document.querySelector("#satellites"),
    hdop: document.querySelector("#hdop"),
    csq: document.querySelector("#csq"),
    deviceBattery: document.querySelector("#device-battery"),
    wakeCode: document.querySelector("#wake-code"),
    trackCount: document.querySelector("#track-count"),
    trackStartTime: document.querySelector("#track-start-time"),
    trackEndTime: document.querySelector("#track-end-time"),
    trackStartPosition: document.querySelector("#track-start-position"),
    trackEndPosition: document.querySelector("#track-end-position"),
    mapPanel: document.querySelector(".map-panel"),
    mapFullscreen: document.querySelector("#map-fullscreen"),
  };

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

  const beijingInputFormatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });

  let map;
  let trackLayer;
  let latestRequestNumber = 0;
  let trackRequestNumber = 0;

  function initializeMap() {
    if (typeof L === "undefined") {
      showMessage("地图组件加载失败，请检查网络连接后刷新页面。", "error");
      return;
    }
    map = L.map("map").setView(DEFAULT_MAP_CENTER, 9);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
  }

  function mapFullscreenAvailable() {
    return typeof document.documentElement.requestFullscreen === "function";
  }

  function mapIsExpanded() {
    return document.fullscreenElement === elements.mapPanel
      || elements.mapPanel.classList.contains("map-panel--expanded");
  }

  function updateFullscreenButton() {
    const expanded = mapIsExpanded();
    elements.mapFullscreen.textContent = expanded ? "退出全屏" : "全屏";
    elements.mapFullscreen.setAttribute("aria-pressed", String(expanded));
  }

  function resizeMap() {
    if (map) map.invalidateSize();
  }

  // 浏览器不支持全屏 API 时（或被 iframe 策略拦截）退回 CSS 铺满屏幕。
  function setMapExpandedByClass(expanded) {
    elements.mapPanel.classList.toggle("map-panel--expanded", expanded);
    document.body.classList.toggle("map-expanded", expanded);
    updateFullscreenButton();
    resizeMap();
  }

  function toggleMapFullscreen() {
    if (mapFullscreenAvailable()) {
      if (document.fullscreenElement === elements.mapPanel) {
        document.exitFullscreen().catch(() => setMapExpandedByClass(false));
      } else {
        elements.mapPanel.requestFullscreen().catch(() => setMapExpandedByClass(true));
      }
      return;
    }
    setMapExpandedByClass(!elements.mapPanel.classList.contains("map-panel--expanded"));
  }

  function beijingMinuteParts(date) {
    return Object.fromEntries(
      beijingInputFormatter.formatToParts(date)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
  }

  function setDefaultRange() {
    const parts = beijingMinuteParts(new Date());
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    const start = `${date}T00:00`;
    let end = `${date}T${parts.hour}:${parts.minute}`;
    if (end <= start) end = `${date}T00:01`;
    elements.trackStart.value = start;
    elements.trackEnd.value = end;
    updateTimeRangeDisplay();
  }

  function formatRangeValue(value) {
    return value ? value.replace("T", " ").replaceAll("-", "/") : "未选择";
  }

  function updateTimeRangeDisplay() {
    elements.timeRangeDisplay.textContent = `${formatRangeValue(elements.trackStart.value)}  ～  ${formatRangeValue(elements.trackEnd.value)}`;
  }

  function setTimeRangePopover(open) {
    elements.timeRangePopover.hidden = !open;
    elements.timeRangeTrigger.setAttribute("aria-expanded", String(open));
  }

  function formatBeijingTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "无效时间"
      : `${beijingTimeFormatter.format(date)}（北京时间 UTC+8）`;
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function validCoordinates(latitude, longitude) {
    return latitude !== null && longitude !== null
      && latitude >= -90 && latitude <= 90
      && longitude >= -180 && longitude <= 180;
  }

  function formatNumber(value, digits = 2, unit = "") {
    const number = finiteNumber(value);
    return number === null ? "—" : `${number.toFixed(digits)}${unit}`;
  }

  function formatInteger(value) {
    const number = finiteNumber(value);
    return number === null ? "—" : String(Math.trunc(number));
  }

  function formatMillivolts(value) {
    const number = finiteNumber(value);
    if (number === null || number <= 0) return "—";
    return `${Math.trunc(number)} mV（${(number / 1000).toFixed(2)} V）`;
  }

  function pointPopupDetails(point) {
    const parts = [];
    const battery = formatMillivolts(point?.battery_mv);
    if (battery !== "—") parts.push(`电量 ${battery}`);
    if (typeof point?.record_seq === "number") parts.push(`记录 #${point.record_seq}`);
    if (point?.time_valid === false) parts.push("时间未校准");
    return parts.length ? `<br>${parts.join(" · ")}` : "";
  }

  function formatPosition(point) {
    const latitude = finiteNumber(point?.latitude);
    const longitude = finiteNumber(point?.longitude);
    return validCoordinates(latitude, longitude)
      ? `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`
      : "—";
  }

  function showMessage(text, type = "info") {
    elements.message.textContent = text;
    elements.message.className = `message${type === "info" ? "" : ` ${type}`}`;
  }

  function redirectToLogin() {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.replace(`/login.html?next=${encodeURIComponent(next)}`);
  }

  async function fetchJson(url, options = {}) {
    let response;
    try {
      response = await fetch(url, {
        ...options,
        headers: { Accept: "application/json", ...(options.headers || {}) },
      });
    } catch (error) {
      throw new Error("无法连接 Tracker API，请检查服务是否正在运行。", { cause: error });
    }
    if (response.status === 401) {
      redirectToLogin();
      throw new Error("登录状态已失效。");
    }
    if (!response.ok) throw new Error(`Tracker API 请求失败（HTTP ${response.status}）。`);
    try {
      return await response.json();
    } catch (error) {
      throw new Error("Tracker API 返回了无法解析的数据。", { cause: error });
    }
  }

  function setOnlineStatus(status) {
    const statuses = {
      online: ["在线", "status-online"],
      warning: ["连接可能中断", "status-warning"],
      offline: ["离线", "status-offline"],
    };
    const [label, className] = statuses[status] || ["未知", "status-unknown"];
    elements.onlineStatus.textContent = label;
    elements.onlineStatus.className = `status-badge ${className}`;
  }

  function resetDeviceDetails() {
    for (const element of [
      elements.deviceImei, elements.deviceName, elements.lastSeen,
      elements.lastGpsTime, elements.validStatus, elements.coordinates,
      elements.altitude, elements.speed, elements.course, elements.satellites,
      elements.hdop, elements.csq, elements.deviceBattery, elements.wakeCode,
    ]) element.textContent = "—";
    setOnlineStatus();
  }

  function renderDevice(device) {
    if (!device || typeof device !== "object") throw new Error("设备详情数据格式无效。");
    elements.deviceImei.textContent = device.imei || "—";
    elements.deviceName.textContent = device.name || "未命名";
    elements.lastSeen.textContent = formatBeijingTime(device.last_seen);
    elements.lastGpsTime.textContent = formatBeijingTime(device.last_gps_time);
    elements.validStatus.textContent = device.valid === true
      ? "有效（A）" : device.valid === false ? "无效（V）" : "—";
    elements.coordinates.textContent = formatPosition(device);
    elements.altitude.textContent = formatNumber(device.altitude, 1, " m");
    elements.speed.textContent = formatNumber(device.speed, 3);
    elements.course.textContent = formatNumber(device.course, 2, "°");
    elements.satellites.textContent = formatInteger(device.satellites);
    elements.hdop.textContent = formatNumber(device.hdop, 2);
    elements.csq.textContent = formatInteger(device.csq);
    elements.deviceBattery.textContent = formatMillivolts(device.battery_mv);
    elements.wakeCode.textContent = formatInteger(device.wake_code);
    setOnlineStatus(device.online_status);
  }

  async function loadDevices() {
    ++latestRequestNumber;
    ++trackRequestNumber;
    clearTrack();
    elements.refreshDevices.disabled = true;
    elements.deviceSelect.disabled = true;
    elements.queryTrack.disabled = true;
    showMessage("正在加载设备列表…");
    elements.deviceSelect.replaceChildren(new Option("正在加载设备…", ""));
    try {
      const devices = await fetchJson("/api/devices");
      if (!Array.isArray(devices)) throw new Error("设备列表数据格式无效。");
      elements.deviceSelect.replaceChildren();
      for (const device of devices) {
        if (device && typeof device.imei === "string" && device.imei) {
          const label = device.name ? `${device.name}（${device.imei}）` : device.imei;
          elements.deviceSelect.append(new Option(label, device.imei));
        }
      }
      if (!elements.deviceSelect.options.length) {
        elements.deviceSelect.append(new Option("暂无设备", ""));
        resetDeviceDetails();
        showMessage("设备列表为空，请等待设备上传数据。");
        return;
      }
      elements.deviceSelect.selectedIndex = 0;
      elements.deviceSelect.disabled = false;
      showMessage(`已加载 ${elements.deviceSelect.options.length} 台设备。`, "success");
      await loadLatest(elements.deviceSelect.value);
    } catch (error) {
      elements.deviceSelect.replaceChildren(new Option("设备加载失败", ""));
      resetDeviceDetails();
      showMessage(error.message || "设备列表加载失败。", "error");
    } finally {
      elements.refreshDevices.disabled = false;
      elements.queryTrack.disabled = !elements.deviceSelect.value;
    }
  }

  async function loadLatest(imei) {
    const requestNumber = ++latestRequestNumber;
    if (!imei) return resetDeviceDetails();
    elements.queryTrack.disabled = true;
    showMessage("正在加载设备最新状态…");
    try {
      const device = await fetchJson(`/api/devices/${encodeURIComponent(imei)}/latest`);
      if (requestNumber !== latestRequestNumber) return;
      renderDevice(device);
      showMessage("设备最新状态已更新。", "success");
    } catch (error) {
      if (requestNumber !== latestRequestNumber) return;
      resetDeviceDetails();
      showMessage(error.message || "设备状态加载失败。", "error");
    } finally {
      if (requestNumber === latestRequestNumber) {
        elements.queryTrack.disabled = !elements.deviceSelect.value;
      }
    }
  }

  function clearTrack() {
    if (map && trackLayer) map.removeLayer(trackLayer);
    trackLayer = null;
    elements.trackCount.textContent = "0";
    elements.trackStartTime.textContent = "—";
    elements.trackEndTime.textContent = "—";
    elements.trackStartPosition.textContent = "—";
    elements.trackEndPosition.textContent = "—";
  }

  function renderTrack(points) {
    clearTrack();
    const ordered = points.filter((point) => point && typeof point === "object")
      .slice().sort((left, right) => Date.parse(left.gps_time) - Date.parse(right.gps_time));
    elements.trackCount.textContent = String(ordered.length);
    if (!ordered.length) return { empty: true, drawableCount: 0 };

    elements.trackStartTime.textContent = formatBeijingTime(ordered[0].gps_time);
    elements.trackEndTime.textContent = formatBeijingTime(ordered.at(-1).gps_time);
    elements.trackStartPosition.textContent = formatPosition(ordered[0]);
    elements.trackEndPosition.textContent = formatPosition(ordered.at(-1));
    const drawable = ordered.filter((point) => validCoordinates(
      finiteNumber(point.latitude), finiteNumber(point.longitude),
    ));
    if (!map || !drawable.length) return { empty: false, drawableCount: drawable.length };

    trackLayer = L.layerGroup().addTo(map);
    const positions = drawable.map((point) => [Number(point.latitude), Number(point.longitude)]);
    if (positions.length === 1) {
      L.marker(positions[0])
        .bindPopup(`轨迹点<br>${formatBeijingTime(drawable[0].gps_time)}${pointPopupDetails(drawable[0])}`)
        .addTo(trackLayer);
      map.setView(positions[0], 16);
    } else {
      const line = L.polyline(positions, { color: TRACK_COLOR, weight: 4, opacity: 0.85 }).addTo(trackLayer);
      L.marker(positions[0])
        .bindPopup(`起点<br>${formatBeijingTime(drawable[0].gps_time)}${pointPopupDetails(drawable[0])}`)
        .addTo(trackLayer);
      L.marker(positions.at(-1))
        .bindPopup(`终点<br>${formatBeijingTime(drawable.at(-1).gps_time)}${pointPopupDetails(drawable.at(-1))}`)
        .addTo(trackLayer);
      map.fitBounds(line.getBounds(), { padding: [32, 32], maxZoom: 17 });
    }
    return { empty: false, drawableCount: positions.length };
  }

  async function queryTrack() {
    const imei = elements.deviceSelect.value;
    const start = elements.trackStart.value;
    const end = elements.trackEnd.value;
    if (!imei) return showMessage("请先选择设备。", "error");
    if (!start || !end) return showMessage("请选择开始和结束时间。", "error");
    if (start >= end) return showMessage("开始时间必须早于结束时间。", "error");

    const requestNumber = ++trackRequestNumber;
    elements.queryTrack.disabled = true;
    showMessage("正在查询轨迹…");
    try {
      const url = `/api/devices/${encodeURIComponent(imei)}/track?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      const points = await fetchJson(url);
      if (requestNumber !== trackRequestNumber) return;
      if (!Array.isArray(points)) throw new Error("轨迹数据格式无效。");
      const result = renderTrack(points);
      if (result.empty) showMessage("所选时间段无轨迹数据。");
      else if (!result.drawableCount) showMessage("轨迹记录没有可绘制的有效坐标。", "error");
      else if (result.drawableCount < points.length) showMessage(`已显示 ${result.drawableCount} 个有效坐标点；部分记录坐标无效。`);
      else showMessage(`已显示 ${result.drawableCount} 个轨迹点。`, "success");
    } catch (error) {
      if (requestNumber !== trackRequestNumber) return;
      clearTrack();
      showMessage(error.message || "轨迹查询失败。", "error");
    } finally {
      if (requestNumber === trackRequestNumber) elements.queryTrack.disabled = false;
    }
  }

  async function logout() {
    elements.logoutButton.disabled = true;
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      window.location.replace("/login.html");
    }
  }

  setDefaultRange();
  initializeMap();
  elements.deviceSelect.addEventListener("change", () => {
    ++trackRequestNumber;
    clearTrack();
    loadLatest(elements.deviceSelect.value);
  });
  elements.queryTrack.addEventListener("click", queryTrack);
  elements.timeRangeTrigger.addEventListener("click", () => {
    setTimeRangePopover(elements.timeRangePopover.hidden);
  });
  elements.trackStart.addEventListener("input", updateTimeRangeDisplay);
  elements.trackEnd.addEventListener("input", updateTimeRangeDisplay);
  elements.timeRangeDone.addEventListener("click", () => setTimeRangePopover(false));
  document.addEventListener("click", (event) => {
    if (!elements.timeRangeField.contains(event.target)) setTimeRangePopover(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!elements.timeRangePopover.hidden) {
      setTimeRangePopover(false);
      elements.timeRangeTrigger.focus();
    } else if (elements.mapPanel.classList.contains("map-panel--expanded")) {
      setMapExpandedByClass(false);
    }
  });
  document.addEventListener("fullscreenchange", () => {
    updateFullscreenButton();
    resizeMap();
  });
  elements.mapFullscreen.addEventListener("click", toggleMapFullscreen);
  elements.refreshDevices.addEventListener("click", loadDevices);
  elements.openDeviceManagement.addEventListener("click", () => {
    window.location.href = "/devices.html";
  });
  elements.logoutButton.addEventListener("click", logout);
  loadDevices();
})();
