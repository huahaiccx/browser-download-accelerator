const DEF = {
  enabled: true,
  port: 6800,
  dir: "",
  conns: 16,
  minSizeMB: 5,
  useCookies: true,
  useReferer: true,
  unknownTakeOver: true,
  blockList: ""
};

const NUM_FIELDS = ["port", "conns", "minSizeMB"];
const BOOL_FIELDS = ["useCookies", "useReferer", "unknownTakeOver"];
const TEXT_FIELDS = ["dir", "blockList"];

function setStatus(msg, ok) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = ok ? "ok" : "bad";
}

async function load() {
  const cfg = Object.assign({}, DEF, await chrome.storage.local.get(DEF));
  NUM_FIELDS.forEach(k => { document.getElementById(k).value = cfg[k]; });
  BOOL_FIELDS.forEach(k => { document.getElementById(k).checked = !!cfg[k]; });
  TEXT_FIELDS.forEach(k => { document.getElementById(k).value = cfg[k] || ""; });
}

async function save() {
  const cfg = {};
  NUM_FIELDS.forEach(k => {
    const v = parseInt(document.getElementById(k).value, 10);
    cfg[k] = Number.isFinite(v) ? v : DEF[k];
  });
  BOOL_FIELDS.forEach(k => { cfg[k] = document.getElementById(k).checked; });
  TEXT_FIELDS.forEach(k => { cfg[k] = document.getElementById(k).value.trim(); });
  if (cfg.conns < 1 || cfg.conns > 32) cfg.conns = 16;
  if (cfg.port < 1 || cfg.port > 65535) cfg.port = 6800;
  await chrome.storage.local.set(cfg);
  setStatus("已保存", true);
}

async function test() {
  const port = parseInt(document.getElementById("port").value, 10) || 6800;
  setStatus("正在测试…", true);
  try {
    const res = await fetch(`http://127.0.0.1:${port}/ping`);
    const data = await res.json();
    setStatus(`连接正常，引擎版本 ${data.version}`, true);
  } catch (e) {
    setStatus("连不上本地引擎：先双击 start-server.cmd 启动服务", false);
  }
}

document.getElementById("save").addEventListener("click", save);
document.getElementById("test").addEventListener("click", test);
load();
