const APP_BASE_URL = new URL(".", document.currentScript?.src || window.location.href);

function appUrl(path) {
  return new URL(String(path || "").replace(/^\/+/, ""), APP_BASE_URL).toString();
}

const DRAWER_PIN_STORAGE_KEY = "z3-panel-command-drawer-pinned";
const DEVICE_ENDPOINTS_STORAGE_KEY = "z3-panel-device-endpoints";

const state = {
  status: null,
  commands: null,
  zigbeeDevices: [],
  zeroCrossStatus: null,
  zigbeeDeviceSignature: "",
  zigbeeDeviceChoiceSignature: "",
  commandDeviceRenderPending: false,
  deviceRefreshTimer: null,
  activeGroup: null,
  selectedCommand: null,
  selectedDeviceRef: null,
  drawerMode: "command",
  commandDrawerPinned: true,
  deviceEndpoints: {},
  params: {},
  browserPath: "",
  logText: "",
};

const $ = (id) => document.getElementById(id);

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2800);
}

async function api(path, options = {}) {
  const response = await fetch(appUrl(path), {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  return data;
}

function saveParams() {
  localStorage.setItem("z3-panel-params", JSON.stringify(state.params));
}

function loadParams(defaults = {}) {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem("z3-panel-params") || "{}");
  } catch {
    localStorage.removeItem("z3-panel-params");
  }
  state.params = { ...defaults, ...saved };
}

function loadCommandDrawerPinned() {
  try {
    const saved = localStorage.getItem(DRAWER_PIN_STORAGE_KEY);
    state.commandDrawerPinned = saved === null ? true : saved === "true";
  } catch {
    state.commandDrawerPinned = true;
  }
}

function saveCommandDrawerPinned() {
  try {
    localStorage.setItem(DRAWER_PIN_STORAGE_KEY, String(state.commandDrawerPinned));
  } catch {
    // Ignore storage failures; the in-memory state still works for this session.
  }
}

function loadDeviceEndpoints() {
  try {
    const saved = JSON.parse(localStorage.getItem(DEVICE_ENDPOINTS_STORAGE_KEY) || "{}");
    state.deviceEndpoints = saved && typeof saved === "object" ? saved : {};
  } catch {
    localStorage.removeItem(DEVICE_ENDPOINTS_STORAGE_KEY);
    state.deviceEndpoints = {};
  }
}

function saveDeviceEndpoints() {
  localStorage.setItem(DEVICE_ENDPOINTS_STORAGE_KEY, JSON.stringify(state.deviceEndpoints));
}

function setInputValue(id, value) {
  const node = $(id);
  if (node && value !== undefined && value !== null) {
    node.value = value;
  }
}

async function refreshStatus() {
  state.status = await api("/api/status");
  const running = state.status.running;
  $("run-pill").textContent = running ? "RUNNING" : "STOPPED";
  $("run-pill").classList.toggle("running", running);
  $("status-line").textContent = running
    ? `PID ${state.status.pid} · ${state.status.serial_port} · session ${state.status.session_id}`
    : `未运行 · last exit ${state.status.last_exit_code ?? "-"}`;
  $("start-btn").disabled = running;
  $("stop-btn").disabled = !running;
  $("send-selected").disabled = !running || !state.selectedCommand;
  $("send-manual").disabled = !running;

  if (!state.browserPath) state.browserPath = state.status.allowed_root || state.status.gateway_root || "/";
  if (!$("executable").value) setInputValue("executable", state.status.executable);
  if (!$("serial-port").value) setInputValue("serial-port", state.status.serial_port || state.status.configured_serial_port || "");
  updateZeroCrossPanel();
  if (!$("network-index").value) setInputValue("network-index", state.status.network_index || "1");
  if (!$("baud-rate").value) setInputValue("baud-rate", state.status.baud_rate || "115200");
}

async function refreshDevices() {
  const data = await api("/api/devices");
  const list = $("serial-list");
  list.innerHTML = "";
  data.devices.forEach((device) => {
    const option = document.createElement("option");
    option.value = device.path;
    option.label = device.realpath && device.realpath !== device.path ? device.realpath : device.path;
    list.appendChild(option);
  });
  if (!$("serial-port").value && data.devices.length > 0) {
    $("serial-port").value = data.devices[0].path;
  }
}

function deviceKey(device) {
  if (!device) return "";
  if (device.role === "gateway") return `gateway:${device.eui64 || device.nodeId || "local"}`;
  if (device.eui64) return `eui64:${device.eui64}`;
  if (device.nodeId) return `node:${device.nodeId}`;
  return "";
}

function deviceRef(device) {
  return {
    key: deviceKey(device),
    nodeId: device.nodeId || "",
    eui64: device.eui64 || "",
    role: device.role || "",
  };
}

function findDeviceByRef(ref) {
  if (!ref) return null;
  if (ref.eui64) {
    const byEui = state.zigbeeDevices.find((device) => device.eui64 === ref.eui64);
    if (byEui) return byEui;
  }
  if (ref.nodeId) {
    const byNode = state.zigbeeDevices.find((device) => device.nodeId === ref.nodeId && device.role === ref.role);
    if (byNode) return byNode;
  }
  return state.zigbeeDevices.find((device) => deviceKey(device) === ref.key) || null;
}

function selectedDevice() {
  return findDeviceByRef(state.selectedDeviceRef);
}

function selectDevice(device) {
  state.drawerMode = "device";
  state.selectedDeviceRef = deviceRef(device);
  state.selectedCommand = null;
  renderCommandList();
  renderZigbeeDevices();
  renderSelectedCommand();
}

function endpointKeyForDevice(device) {
  return device?.eui64 || device?.nodeId || "default";
}

function endpointForDevice(device) {
  return state.deviceEndpoints[endpointKeyForDevice(device)] || "1";
}

function updateDeviceEndpoint(device, value) {
  state.deviceEndpoints[endpointKeyForDevice(device)] = value.trim() || "1";
  saveDeviceEndpoints();
}

function canOperateDevice(device) {
  return Boolean(device && device.role !== "gateway" && device.nodeId);
}

function deviceLabel(device) {
  const node = device.nodeId || "node?";
  const eui = device.eui64 || "eui64?";
  const lqi = device.lqi === null || device.lqi === undefined ? "LQI -" : `LQI ${device.lqi}`;
  if (device.role === "gateway") return `网关 · ${node} · ${eui}`;
  return `${node} · ${eui} · ${lqi}`;
}

function deviceValueFor(device, valueType) {
  if (valueType === "nodeId") return device.role === "gateway" ? "" : device.nodeId || "";
  if (valueType === "eui64Braced") return device.eui64 ? `{${device.eui64}}` : "";
  return "";
}

function zigbeeDevicesSignature(devices) {
  return JSON.stringify((devices || []).map((device) => ({
    nodeId: device.nodeId || "",
    eui64: device.eui64 || "",
    lqi: device.lqi ?? null,
    lastSeen: device.lastSeen || "",
    role: device.role || "",
    name: device.name || "",
  })));
}

function zigbeeDeviceChoiceSignature(devices) {
  return JSON.stringify((devices || []).map((device) => ({
    nodeId: device.nodeId || "",
    eui64: device.eui64 || "",
    role: device.role || "",
  })));
}

function isEditingCommandForm() {
  const form = $("param-form");
  return Boolean(form && form.contains(document.activeElement));
}

function renderPendingCommandDevicesIfIdle() {
  if (!state.commandDeviceRenderPending || isEditingCommandForm()) return;
  state.commandDeviceRenderPending = false;
  renderSelectedCommand();
}

async function refreshZeroCrossStatus() {
  state.zeroCrossStatus = await api("/api/zerocross/status");
  updateZeroCrossPanel();
  return state.zeroCrossStatus;
}

async function loadZigbeeDevices({ reparse = false } = {}) {
  const data = reparse
    ? await api("/api/zigbee/devices/reparse", { method: "POST", body: JSON.stringify({}) })
    : await api("/api/zigbee/devices");
  const devices = data.devices || [];
  const nextSignature = zigbeeDevicesSignature(devices);
  const nextChoiceSignature = zigbeeDeviceChoiceSignature(devices);
  const changed = nextSignature !== state.zigbeeDeviceSignature;
  const choicesChanged = nextChoiceSignature !== state.zigbeeDeviceChoiceSignature;
  state.zigbeeDevices = devices;
  state.zigbeeDeviceSignature = nextSignature;
  state.zigbeeDeviceChoiceSignature = nextChoiceSignature;
  if (!changed && !reparse) return;

  renderZigbeeDevices();
  if (state.drawerMode === "device") {
    if (isEditingCommandForm()) {
      state.commandDeviceRenderPending = true;
      return;
    }
    renderSelectedCommand();
  }
  if (!choicesChanged && !reparse) return;
  if (isEditingCommandForm()) {
    state.commandDeviceRenderPending = true;
    return;
  }
  state.commandDeviceRenderPending = false;
  renderSelectedCommand();
}

function scheduleZigbeeDeviceRefresh() {
  clearTimeout(state.deviceRefreshTimer);
  state.deviceRefreshTimer = setTimeout(() => {
    loadZigbeeDevices().catch(() => {});
  }, 900);
}

function mayContainZigbeeDeviceChange(text) {
  return /Trust Center Join Handler|Device Announce|RX:\s*ZDO,\s*command\s+0x8034|node \[\(>\)[0-9A-Fa-f]{16}\]|nodeID \[0x[0-9A-Fa-f]{1,4}\]/i.test(text)
    || /^\s*\d+:\s+0x[0-9A-Fa-f]{4}\s+\d+\s+/m.test(text);
}

function renderZigbeeDevices() {
  const list = $("zigbee-device-list");
  if (!list) return;
  list.innerHTML = "";
  if (!state.zigbeeDevices.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无设备。开放入网后会自动记录。";
    list.appendChild(empty);
    return;
  }
  const activeKey = deviceKey(selectedDevice());
  state.zigbeeDevices.forEach((device) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = ["zigbee-device", deviceKey(device) === activeKey ? "active" : ""].filter(Boolean).join(" ");
    item.addEventListener("click", () => selectDevice(device));

    const title = document.createElement("strong");
    title.textContent = device.role === "gateway" ? "网关" : device.nodeId || "未知短地址";

    const eui = document.createElement("code");
    eui.textContent = device.eui64 || "未知 EUI64";

    const meta = document.createElement("div");
    meta.className = "zigbee-device-meta";
    const lqi = device.lqi === null || device.lqi === undefined ? "LQI -" : `LQI ${device.lqi}`;
    const seen = device.lastSeen ? device.lastSeen.replace("T", " ") : "未记录时间";
    meta.textContent = device.role === "gateway" ? `协调器 · ${seen}` : `${lqi} · ${seen}`;

    item.append(title, eui, meta);
    list.appendChild(item);
  });
}

function renderGroups() {
  const tabs = $("group-tabs");
  tabs.innerHTML = "";
  const groups = state.commands.groups || [];
  if (!state.activeGroup && groups.length) {
    state.activeGroup = state.commands.default_group || groups[0].name;
  }
  groups.forEach((group) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tab ${group.name === state.activeGroup ? "active" : ""}`;
    button.textContent = group.name;
    button.addEventListener("click", () => {
      state.activeGroup = group.name;
      state.selectedCommand = null;
      state.selectedDeviceRef = null;
      state.drawerMode = "command";
      renderGroups();
      renderCommandList();
      renderSelectedCommand();
    });
    tabs.appendChild(button);
  });
}

function commandMatchesFilter(command, filter) {
  if (!filter) return true;
  const haystack = `${command.label} ${command.command}`.toLowerCase();
  return haystack.includes(filter.toLowerCase());
}

function renderCommandList() {
  const list = $("command-list");
  list.innerHTML = "";
  const filter = $("command-filter").value.trim();
  const group = (state.commands.groups || []).find((item) => item.name === state.activeGroup);
  if (!group) return;

  group.commands.filter((cmd) => commandMatchesFilter(cmd, filter)).forEach((cmd) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = [
      "command-button",
      state.selectedCommand?.id === cmd.id ? "active" : "",
      cmd.danger ? "dangerous" : "",
    ].filter(Boolean).join(" ");
    button.textContent = cmd.label;
    button.title = cmd.command;
    button.addEventListener("click", () => {
      state.selectedCommand = cmd;
      state.selectedDeviceRef = null;
      state.drawerMode = "command";
      hydrateMissingParams(cmd);
      renderZigbeeDevices();
      renderCommandList();
      renderSelectedCommand();
    });
    list.appendChild(button);
  });
}

function fieldsForCommand(command) {
  if (command.fields && command.fields.length) return command.fields;
  const keys = [...command.command.matchAll(/\{\{([a-zA-Z0-9_]+)\}\}/g)].map((match) => match[1]);
  return [...new Set(keys)].map((key) => ({ key, label: key }));
}

function hydrateMissingParams(command) {
  fieldsForCommand(command).forEach((field) => {
    if (state.params[field.key] === undefined) {
      state.params[field.key] = field.default || "";
    }
  });
}

function parseIntegerParam(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^0x[0-9a-f]+$/i.test(text)) return Number.parseInt(text, 16);
  if (/^[0-9]+$/.test(text)) return Number.parseInt(text, 10);
  return null;
}

function toHexByte(value) {
  return value.toString(16).toUpperCase().padStart(2, "0");
}

function encodedFieldValue(field) {
  const raw = state.params[field.key] ?? field.default ?? "";
  if (field.encode === "uint16HexBytes") {
    const value = parseIntegerParam(raw);
    if (value === null || value < 0 || value > 0xffff) return "";
    return `${toHexByte((value >> 8) & 0xff)} ${toHexByte(value & 0xff)}`;
  }
  return raw;
}

function commandValues(command) {
  const values = { ...state.params };
  fieldsForCommand(command).forEach((field) => {
    if (field.outputKey) {
      values[field.outputKey] = encodedFieldValue(field);
    }
  });
  return values;
}

function renderCommand(command) {
  const values = commandValues(command);
  return command.command.replace(/\{\{([a-zA-Z0-9_]+)\}\}/g, (_, key) => values[key] ?? "");
}

function missingFields(command) {
  return fieldsForCommand(command)
    .filter((field) => {
      if (String(state.params[field.key] ?? "").trim() === "") return true;
      if (field.outputKey && encodedFieldValue(field) === "") return true;
      return false;
    })
    .map((field) => field.label || field.key);
}

function updateParamValue(key, value, command) {
  state.params[key] = value;
  saveParams();
  $("command-preview").value = renderCommand(command);
}

function shouldCommandDrawerOpen() {
  return state.commandDrawerPinned || Boolean(state.selectedCommand) || Boolean(state.selectedDeviceRef);
}

function setCommandDrawerOpen(open) {
  const layout = document.querySelector(".layout");
  if (layout) layout.classList.toggle("drawer-open", Boolean(open));
}

function renderCommandDrawerPin() {
  const button = $("toggle-command-drawer-pin");
  if (!button) return;
  button.textContent = state.commandDrawerPinned ? "已钉住" : "钉住";
  button.title = state.commandDrawerPinned ? "点击后详情栏可自动收起" : "点击后固定显示详情栏";
  button.setAttribute("aria-pressed", String(state.commandDrawerPinned));
  button.classList.toggle("active", state.commandDrawerPinned);
}

function toggleCommandDrawerPinned() {
  state.commandDrawerPinned = !state.commandDrawerPinned;
  saveCommandDrawerPinned();
  renderSelectedCommand();
}

function closeCommandDrawer({ unpin = false } = {}) {
  if (unpin && state.commandDrawerPinned) {
    state.commandDrawerPinned = false;
    saveCommandDrawerPinned();
  }
  state.selectedCommand = null;
  state.selectedDeviceRef = null;
  state.drawerMode = "command";
  renderZigbeeDevices();
  renderCommandList();
  renderSelectedCommand();
}

function renderParamField(field, command) {
  const label = document.createElement("label");
  label.className = "field";
  const text = document.createElement("span");
  text.textContent = field.label || field.key;
  const input = document.createElement("input");
  input.value = state.params[field.key] ?? field.default ?? "";
  input.placeholder = field.placeholder || "";
  if (field.inputType) input.type = field.inputType;
  if (field.min !== undefined) input.min = field.min;
  if (field.max !== undefined) input.max = field.max;
  if (field.step !== undefined) input.step = field.step;
  input.addEventListener("input", () => updateParamValue(field.key, input.value, command));

  if (!field.deviceValue) {
    label.append(text, input);
    return label;
  }

  const control = document.createElement("div");
  control.className = "device-field-control";
  const select = document.createElement("select");
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = state.zigbeeDevices.length ? "选择设备" : "暂无设备";
  select.appendChild(empty);

  state.zigbeeDevices.forEach((device) => {
    const value = deviceValueFor(device, field.deviceValue);
    if (!value) return;
    const option = document.createElement("option");
    option.value = value;
    option.textContent = deviceLabel(device);
    if (value === input.value) option.selected = true;
    select.appendChild(option);
  });

  select.disabled = !state.zigbeeDevices.length;
  select.addEventListener("change", () => {
    if (!select.value) return;
    input.value = select.value;
    updateParamValue(field.key, input.value, command);
  });

  control.append(input, select);
  label.append(text, control);
  return label;
}

function setDrawerFooterForMode(mode) {
  const sendButton = $("send-selected");
  if (!sendButton) return;
  sendButton.style.display = mode === "device" ? "none" : "";
  if (mode !== "device") sendButton.textContent = "发送选中命令";
}

function renderDeviceDefaultPreview(device) {
  const preview = $("command-preview");
  if (!preview) return;
  if (!canOperateDevice(device)) {
    preview.value = "";
    return;
  }
  preview.value = deviceOnOffLines(device, "on").join("\n");
}

function renderDeviceDetail(device) {
  setDrawerFooterForMode("device");
  setCommandDrawerOpen(shouldCommandDrawerOpen());
  renderCommandDrawerPin();
  $("selected-title").textContent = device
    ? `设备详情 · ${device.role === "gateway" ? "网关" : device.nodeId || "未知"}`
    : "设备详情";
  const form = $("param-form");
  form.innerHTML = "";

  if (!device) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "设备记录已更新，当前选中的设备不存在。";
    form.appendChild(empty);
    $("command-preview").value = "";
    return;
  }

  const summary = document.createElement("div");
  summary.className = "device-detail-summary";
  [
    ["短地址", device.nodeId || "未知"],
    ["EUI64", device.eui64 || "未知"],
    ["LQI", device.lqi === null || device.lqi === undefined ? "-" : String(device.lqi)],
    ["最后出现", device.lastSeen ? device.lastSeen.replace("T", " ") : "未记录"],
  ].forEach(([label, value]) => {
    const row = document.createElement("div");
    const labelNode = document.createElement("span");
    labelNode.textContent = label;
    const valueNode = document.createElement("code");
    valueNode.textContent = value;
    row.append(labelNode, valueNode);
    summary.appendChild(row);
  });
  form.appendChild(summary);

  const endpointField = document.createElement("label");
  endpointField.className = "field";
  const endpointLabel = document.createElement("span");
  endpointLabel.textContent = "Endpoint";
  const endpointInput = document.createElement("input");
  endpointInput.inputMode = "numeric";
  endpointInput.value = endpointForDevice(device);
  endpointInput.addEventListener("input", () => {
    updateDeviceEndpoint(device, endpointInput.value);
    renderDeviceDefaultPreview(device);
  });
  endpointField.append(endpointLabel, endpointInput);
  form.appendChild(endpointField);

  if (device.role === "gateway") {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "网关记录用于绑定下拉选择，不作为开关/校准目标。";
    form.appendChild(note);
    $("command-preview").value = "";
    return;
  }

  if (!device.nodeId) {
    const note = document.createElement("p");
    note.className = "muted warning-text";
    note.textContent = "缺少短地址，先刷新邻居表后才能直接下发单播命令。";
    form.appendChild(note);
  }

  const sectionTitle = document.createElement("h4");
  sectionTitle.className = "device-section-title";
  sectionTitle.textContent = "开关控制";
  form.appendChild(sectionTitle);

  const onOffRow = document.createElement("div");
  onOffRow.className = "device-action-grid three";
  [
    ["开", "on"],
    ["关", "off"],
    ["Toggle", "toggle"],
  ].forEach(([label, action]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = action === "off" ? "danger" : "primary";
    button.textContent = label;
    button.disabled = !canOperateDevice(device);
    button.addEventListener("click", () => sendDeviceAction(device, deviceOnOffLines(device, action), label));
    onOffRow.appendChild(button);
  });
  form.appendChild(onOffRow);

  const calibrationTitle = document.createElement("h4");
  calibrationTitle.className = "device-section-title";
  calibrationTitle.textContent = "过零校准";
  form.appendChild(calibrationTitle);

  const calibrationGrid = document.createElement("div");
  calibrationGrid.className = "device-calibration-grid";
  calibrationGrid.appendChild(renderCalibrationControl(device, "on", "开灯时间 us", "zeroCrossOnUs", "FA"));
  calibrationGrid.appendChild(renderCalibrationControl(device, "off", "关灯时间 us", "zeroCrossOffUs", "FB"));
  form.appendChild(calibrationGrid);
  form.appendChild(renderAutoZeroCrossControl(device));
  updateZeroCrossPanel();

  const refreshButton = document.createElement("button");
  refreshButton.type = "button";
  refreshButton.className = "ghost wide-button";
  refreshButton.textContent = "刷新邻居表";
  refreshButton.addEventListener("click", async () => {
    if (!state.status?.running) {
      toast("gateway 未运行，不能发送命令");
      return;
    }
    const lines = ["plugin stack-diagnostics neighbor-table"];
    $("command-preview").value = lines.join("\n");
    await sendLines(lines, "刷新邻居表已发送");
  });
  form.appendChild(refreshButton);

  const leaveButton = document.createElement("button");
  leaveButton.type = "button";
  leaveButton.className = "danger wide-button";
  leaveButton.textContent = "踢设备离网";
  leaveButton.disabled = !canOperateDevice(device);
  leaveButton.addEventListener("click", () => leaveSelectedDevice(device).catch((err) => toast(err.message)));
  form.appendChild(leaveButton);

  renderDeviceDefaultPreview(device);
}

function renderCalibrationControl(device, kind, label, paramKey, commandByte) {
  const box = document.createElement("div");
  box.className = "device-calibration-card";

  const field = document.createElement("label");
  field.className = "field";
  const span = document.createElement("span");
  span.textContent = label;
  const input = document.createElement("input");
  input.inputMode = "numeric";
  input.value = state.params[paramKey] ?? (kind === "on" ? "300" : "1");
  input.addEventListener("input", () => {
    state.params[paramKey] = input.value;
    saveParams();
  });
  field.append(span, input);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "primary wide-button";
  button.textContent = kind === "on" ? "发送开灯校准" : "发送关灯校准";
  button.disabled = !canOperateDevice(device);
  button.addEventListener("click", () => {
    const bytes = uint16HexBytes(input.value);
    if (!bytes) {
      toast("过零时间需要是 0-65535 的整数");
      return;
    }
    sendDeviceAction(device, deviceZeroCrossLines(device, commandByte, bytes), button.textContent);
  });

  box.append(field, button);
  return box;
}

function renderAutoZeroCrossControl(device) {
  const box = document.createElement("div");
  box.className = "auto-zero-cross-card";

  const header = document.createElement("div");
  header.className = "auto-zero-cross-header";
  const title = document.createElement("strong");
  title.textContent = "自动过零校准";
  const stateNode = document.createElement("span");
  stateNode.id = "zc-result";
  stateNode.className = "zc-result";
  stateNode.textContent = "未启动";
  header.append(title, stateNode);

  const serial = document.createElement("p");
  serial.id = "zc-serial";
  serial.className = "muted";
  serial.textContent = "仪器串口：读取中";

  const metrics = document.createElement("div");
  metrics.className = "auto-zero-cross-metrics";
  [
    ["轮次", "zc-round"],
    ["开灯测量", "zc-on-measurement"],
    ["开灯补偿", "zc-on-calibration"],
    ["开灯状态", "zc-on-status"],
    ["关灯测量", "zc-off-measurement"],
    ["关灯补偿", "zc-off-calibration"],
    ["关灯状态", "zc-off-status"],
    ["超时", "zc-timeouts"],
  ].forEach(([label, id]) => {
    const row = document.createElement("div");
    const key = document.createElement("span");
    key.textContent = label;
    const value = document.createElement("code");
    value.id = id;
    value.textContent = "-";
    row.append(key, value);
    metrics.appendChild(row);
  });

  const error = document.createElement("p");
  error.id = "zc-error";
  error.className = "muted warning-text hidden-line";

  const actions = document.createElement("div");
  actions.className = "device-action-grid two";
  const start = document.createElement("button");
  start.id = "zc-start";
  start.type = "button";
  start.className = "primary";
  start.textContent = "开始自动校准";
  start.addEventListener("click", () => startAutoZeroCross(device).catch((err) => toast(err.message)));
  const stop = document.createElement("button");
  stop.id = "zc-stop";
  stop.type = "button";
  stop.className = "danger";
  stop.textContent = "停止校准";
  stop.addEventListener("click", () => stopAutoZeroCross().catch((err) => toast(err.message)));
  actions.append(start, stop);

  box.append(header, serial, metrics, error, actions);
  return box;
}

function formatUs(value) {
  return value === null || value === undefined ? "-" : `${value} us`;
}

function zeroCrossResultLabel(result, active) {
  if (active) return "运行中";
  const labels = {
    idle: "未启动",
    success: "已完成",
    stopped: "已停止",
    "user-stop": "已停止",
    "gateway-stop": "已停止",
    "server-stop": "已停止",
    timeout: "超时停止",
    "max-rounds": "达到轮数上限",
    error: "错误",
  };
  return labels[result] || result || "未启动";
}

function zeroCrossStatusLabel(status) {
  const labels = {
    success: "达标",
    adjusted: "已补偿",
    timeout: "超时",
  };
  return labels[status] || status || "-";
}

function updateZeroCrossPanel() {
  const resultNode = $("zc-result");
  if (!resultNode) return;
  const zc = state.zeroCrossStatus || {};
  const device = selectedDevice();
  const active = Boolean(zc.active);
  resultNode.textContent = zeroCrossResultLabel(zc.result, active);
  resultNode.className = ["zc-result", active ? "active" : "", zc.result === "success" ? "success" : "", zc.result === "error" || zc.result === "timeout" ? "danger" : ""].filter(Boolean).join(" ");

  const serialText = zc.serial_port || zc.configured_serial_port || "未配置";
  $("zc-serial").textContent = `仪器串口：${serialText} · ${zc.serial_open ? "已打开" : "未打开"} · ${zc.serial_baud_rate || 9600} baud`;
  $("zc-round").textContent = `${zc.round || 0} / ${zc.max_rounds || 20}`;
  $("zc-on-measurement").textContent = formatUs(zc.last_on_measurement_us);
  $("zc-off-measurement").textContent = formatUs(zc.last_off_measurement_us);
  $("zc-on-calibration").textContent = formatUs(zc.last_on_calibration_us);
  $("zc-off-calibration").textContent = formatUs(zc.last_off_calibration_us);
  $("zc-on-status").textContent = zeroCrossStatusLabel(zc.last_on_status);
  $("zc-off-status").textContent = zeroCrossStatusLabel(zc.last_off_status);
  $("zc-timeouts").textContent = String(zc.consecutive_timeouts || 0);
  const error = $("zc-error");
  error.textContent = zc.last_error ? `错误：${zc.last_error}` : "";
  error.classList.toggle("hidden-line", !zc.last_error);

  const canStart = state.status?.running && canOperateDevice(device) && !active;
  const startButton = $("zc-start");
  const stopButton = $("zc-stop");
  if (startButton) startButton.disabled = !canStart;
  if (stopButton) stopButton.disabled = !active;
}

async function startAutoZeroCross(device) {
  if (!state.status?.running) {
    toast("gateway 未运行，不能自动校准");
    return;
  }
  if (!canOperateDevice(device)) {
    toast("当前设备缺少短地址，不能自动校准");
    return;
  }
  const endpoint = endpointForDevice(device);
  state.zeroCrossStatus = await api("/api/zerocross/start", {
    method: "POST",
    body: JSON.stringify({ nodeId: device.nodeId, endpoint }),
  });
  $("command-preview").value = `自动过零校准\nnode ${device.nodeId}\nendpoint ${endpoint}`;
  updateZeroCrossPanel();
  toast("自动过零校准已启动");
}

async function stopAutoZeroCross() {
  state.zeroCrossStatus = await api("/api/zerocross/stop", { method: "POST", body: JSON.stringify({}) });
  updateZeroCrossPanel();
  toast("自动过零校准已停止");
}

function renderSelectedCommand() {
  if (state.drawerMode === "device") {
    renderDeviceDetail(selectedDevice());
    return;
  }

  setDrawerFooterForMode("command");
  const cmd = state.selectedCommand;
  setCommandDrawerOpen(shouldCommandDrawerOpen());
  renderCommandDrawerPin();
  $("selected-title").textContent = cmd ? cmd.label : "未选择命令";
  $("param-form").innerHTML = "";
  $("command-preview").value = cmd ? renderCommand(cmd) : "";
  $("send-selected").disabled = !state.status?.running || !cmd;
  if (!cmd) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "从中间选择一条命令，参数会显示在这里。";
    $("param-form").appendChild(empty);
    return;
  }

  const fields = fieldsForCommand(cmd);
  if (!fields.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "这条命令没有参数。";
    $("param-form").appendChild(empty);
    return;
  }

  fields.forEach((field) => {
    $("param-form").appendChild(renderParamField(field, cmd));
  });
}

async function loadCommands() {
  state.commands = await api("/api/commands");
  loadParams(state.commands.parameter_defaults || {});
  renderGroups();
  renderCommandList();
  renderSelectedCommand();
}

async function browse(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path || state.browserPath)}`);
  state.browserPath = data.path;
  setBrowserPath(data);
  renderBrowserList("browser-list", data);
  renderBrowserList("file-picker-list", data);
  return data;
}

function setBrowserPath(data) {
  $("browser-path").value = data.path;
  $("browser-up").disabled = !data.parent;
  $("browser-up").dataset.parent = data.parent || "";

  const pickerPath = $("file-picker-path");
  const pickerUp = $("file-picker-up");
  if (pickerPath) pickerPath.value = data.path;
  if (pickerUp) {
    pickerUp.disabled = !data.parent;
    pickerUp.dataset.parent = data.parent || "";
  }
}

function renderBrowserList(containerId, data) {
  const list = $(containerId);
  if (!list) return;
  list.innerHTML = "";
  data.entries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "browser-row";
    const kind = document.createElement("div");
    kind.className = "kind";
    kind.textContent = entry.is_dir ? "DIR" : entry.is_executable ? "EXE" : "FILE";
    const name = document.createElement("button");
    name.type = "button";
    name.className = "ghost name";
    name.textContent = entry.name;
    name.disabled = entry.blocked || (!entry.is_dir && !entry.is_executable);
    name.addEventListener("click", () => {
      if (entry.is_dir) browse(entry.path).catch((err) => toast(err.message));
      else if (entry.is_executable) selectExecutable(entry.path);
    });
    const action = document.createElement("button");
    action.type = "button";
    action.textContent = entry.is_dir ? "打开" : "选择";
    action.disabled = entry.blocked || (!entry.is_dir && !entry.is_executable);
    action.addEventListener("click", () => name.click());
    row.append(kind, name, action);
    list.appendChild(row);
  });
}

function selectExecutable(path) {
  $("executable").value = path;
  closeFilePicker();
  toast("已选择可执行文件：" + path);
}

function openFilePicker(path) {
  const modal = $("file-picker");
  modal.classList.remove("hidden");
  return browse(path || $("executable").value || state.browserPath)
    .then(() => $("file-picker-path").focus());
}

function closeFilePicker() {
  const modal = $("file-picker");
  if (modal) modal.classList.add("hidden");
}

async function startGateway() {
  const payload = {
    executable: $("executable").value.trim(),
    serial_port: $("serial-port").value.trim(),
    network_index: $("network-index").value.trim(),
    baud_rate: $("baud-rate").value.trim(),
  };
  await api("/api/start", { method: "POST", body: JSON.stringify(payload) });
  toast("gateway 已启动");
  await refreshStatus();
}

async function stopGateway() {
  await api("/api/stop", { method: "POST", body: JSON.stringify({}) });
  toast("停止请求已发送");
  await refreshStatus();
}

async function sendLine(command) {
  await api("/api/send", { method: "POST", body: JSON.stringify({ command }) });
}

async function sendLines(lines, successMessage = "命令已发送") {
  const cleanLines = lines.map((line) => line.trim()).filter(Boolean);
  if (!cleanLines.length) return;
  for (const line of cleanLines) {
    await sendLine(line);
  }
  toast(successMessage || (cleanLines.length === 1 ? "命令已发送" : `${cleanLines.length} 条命令已发送`));
}

function uint16HexBytes(value) {
  const parsed = parseIntegerParam(value);
  if (parsed === null || parsed < 0 || parsed > 0xffff) return "";
  return `${toHexByte((parsed >> 8) & 0xff)} ${toHexByte(parsed & 0xff)}`;
}

function deviceOnOffLines(device, action) {
  const endpoint = endpointForDevice(device);
  return [`zcl on-off ${action}`, `send ${device.nodeId} ${endpoint} ${endpoint}`];
}

function deviceZeroCrossLines(device, commandByte, bytes) {
  const endpoint = endpointForDevice(device);
  return [`raw 0xEEEE {11 01 ${commandByte} ${bytes}}`, `send ${device.nodeId} ${endpoint} ${endpoint}`];
}

async function sendDeviceAction(device, lines, label) {
  if (!state.status?.running) {
    toast("gateway 未运行，不能发送命令");
    return;
  }
  if (!canOperateDevice(device)) {
    toast("当前设备缺少短地址，不能直接单播发送");
    return;
  }
  const cleanLines = lines.map((line) => line.trim()).filter(Boolean);
  $("command-preview").value = cleanLines.join("\n");
  await sendLines(cleanLines, `${label} 已发送`);
}

async function leaveSelectedDevice(device) {
  if (!canOperateDevice(device)) {
    toast("当前设备缺少短地址，不能发送离网命令");
    return;
  }
  if (!confirm(`让设备离网？\n\n${device.nodeId}\n${device.eui64 || ""}`)) return;
  await sendDeviceAction(device, [`zdo leave ${device.nodeId} 0 0`], "设备离网");
}

async function sendSelected() {
  const cmd = state.selectedCommand;
  if (!cmd) return;
  const missing = missingFields(cmd);
  if (missing.length) {
    toast(`缺少参数：${missing.join(", ")}`);
    return;
  }
  const commandText = renderCommand(cmd);
  if (cmd.danger && !confirm(`发送危险命令？\n\n${commandText}`)) return;
  const lines = commandText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  await sendLines(lines, lines.length === 1 ? "命令已发送" : `${lines.length} 条命令已发送`);
}

async function sendManual() {
  const lines = $("manual-command").value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) return;
  await sendLines(lines, lines.length === 1 ? "命令已发送" : `${lines.length} 条命令已发送`);
  $("manual-command").value = "";
}

function appendLog(text) {
  state.logText += text;
  const output = $("log-output");
  output.textContent = state.logText;
  output.scrollTop = output.scrollHeight;
}

function connectLogs() {
  const events = new EventSource(appUrl("/api/logs/stream"));
  events.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      const text = payload.text || "";
      appendLog(text);
      if (payload.kind === "output" && mayContainZigbeeDeviceChange(text)) {
        scheduleZigbeeDeviceRefresh();
      }
      if (payload.kind === "system" && text.includes("[zerocross]")) {
        refreshZeroCrossStatus().catch(() => {});
      }
    } catch {
      appendLog(event.data);
    }
  };
  events.onerror = () => {
    $("status-line").textContent = "日志连接中断，浏览器会自动重连";
  };
}

function searchLog() {
  const term = $("log-search").value;
  if (!term) return;
  const output = $("log-output");
  const index = output.textContent.toLowerCase().indexOf(term.toLowerCase());
  if (index < 0) {
    toast("未找到");
    return;
  }
  const ratio = index / Math.max(output.textContent.length, 1);
  output.scrollTop = ratio * output.scrollHeight;
}

function wireEvents() {
  $("refresh-devices").addEventListener("click", () => refreshDevices().catch((err) => toast(err.message)));
  $("toggle-command-drawer-pin").addEventListener("click", toggleCommandDrawerPinned);
  $("close-command-drawer").addEventListener("click", () => closeCommandDrawer({ unpin: true }));
  $("param-form").addEventListener("focusout", () => {
    setTimeout(renderPendingCommandDevicesIfIdle, 0);
  });
  $("reparse-devices").addEventListener("click", () => {
    loadZigbeeDevices({ reparse: true })
      .then(() => toast("设备记录已从历史日志重扫"))
      .catch((err) => toast(err.message));
  });
  $("start-btn").addEventListener("click", () => startGateway().catch((err) => toast(err.message)));
  $("stop-btn").addEventListener("click", () => stopGateway().catch((err) => toast(err.message)));
  $("browse-default").addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openFilePicker($("executable").value || state.browserPath).catch((err) => toast(err.message));
  });
  $("browser-go").addEventListener("click", () => openFilePicker($("browser-path").value).catch((err) => toast(err.message)));
  $("browser-up").addEventListener("click", () => browse($("browser-up").dataset.parent).catch((err) => toast(err.message)));
  $("file-picker-close").addEventListener("click", closeFilePicker);
  $("file-picker-backdrop").addEventListener("click", closeFilePicker);
  $("file-picker-go").addEventListener("click", () => browse($("file-picker-path").value).catch((err) => toast(err.message)));
  $("file-picker-up").addEventListener("click", () => browse($("file-picker-up").dataset.parent).catch((err) => toast(err.message)));
  $("file-picker-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") browse($("file-picker-path").value).catch((err) => toast(err.message));
    if (event.key === "Escape") closeFilePicker();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeFilePicker();
    if ((state.selectedCommand || state.selectedDeviceRef) && !state.commandDrawerPinned) closeCommandDrawer();
  });
  $("send-selected").addEventListener("click", () => sendSelected().catch((err) => toast(err.message)));
  $("send-manual").addEventListener("click", () => sendManual().catch((err) => toast(err.message)));
  $("command-filter").addEventListener("input", renderCommandList);
  $("clear-log").addEventListener("click", () => {
    state.logText = "";
    $("log-output").textContent = "";
  });
  $("copy-log").addEventListener("click", async () => {
    await navigator.clipboard.writeText($("log-output").textContent);
    toast("日志已复制");
  });
  $("log-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchLog();
  });
}

async function init() {
  loadCommandDrawerPinned();
  loadDeviceEndpoints();
  wireEvents();
  await Promise.all([refreshStatus(), refreshDevices(), loadCommands(), loadZigbeeDevices(), refreshZeroCrossStatus()]);
  if (state.status.default_executable) {
    $("executable").value = state.status.default_executable;
  }
  await browse(state.status.allowed_root);
  connectLogs();
  setInterval(() => refreshStatus().catch(() => {}), 3000);
  setInterval(() => refreshZeroCrossStatus().catch(() => {}), 1500);
}

init().catch((err) => toast(err.message));
