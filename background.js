// 拦截浏览器下载 -> 转交本地引擎 -> 取消浏览器自己那条
const DEF = {
  enabled: true,
  port: 6800,
  dir: "",            // 留空 = 引擎默认目录（用户的 下载 文件夹）
  conns: 16,
  minSizeMB: 5,       // 小于这个体积不接管，交给浏览器
  useCookies: true,   // 带上站点 Cookie，登录态下载也能下
  useReferer: true,
  unknownTakeOver: true, // 拿不到文件大小时仍然接管
  blockList: ""       // 每行一个域名，命中的站点不接管
};

async function getCfg() {
  const got = await chrome.storage.local.get(DEF);
  return Object.assign({}, DEF, got);
}

async function post(path, payload) {
  const cfg = await getCfg();
  const res = await fetch(`http://127.0.0.1:${cfg.port}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function sanitize(name) {
  return (name || "").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim().slice(0, 150);
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
  } catch (e) {}
}

// 等一小会儿拿文件大小（多数站点 Content-Length 一次就送到）
async function waitSize(id, maxMs = 1500) {
  const t0 = Date.now();
  let item = null;
  while (Date.now() - t0 < maxMs) {
    const [it] = await chrome.downloads.search({ id });
    if (!it) return { item: null, size: -1 };
    item = it;
    const size = it.totalBytes > 0 ? it.totalBytes : (it.fileSize > 0 ? it.fileSize : -1);
    if (size > 0) return { item, size };
    if (it.state !== "in_progress") return { item, size };
    await new Promise(r => setTimeout(r, 200));
  }
  const size = item && (item.totalBytes > 0 ? item.totalBytes : (item.fileSize > 0 ? item.fileSize : -1));
  return { item, size: size || -1 };
}

async function buildHeaders(cfg, item) {
  const url = item.finalUrl || item.url;
  const headers = { "User-Agent": navigator.userAgent };
  if (cfg.useCookies) {
    try {
      const cs = await chrome.cookies.getAll({ url });
      if (cs && cs.length) {
        headers["Cookie"] = cs.map(c => `${c.name}=${c.value}`).join("; ");
      }
    } catch (e) {}
  }
  if (cfg.useReferer && /^http/i.test(item.referrer || "")) {
    headers["Referer"] = item.referrer;
  }
  return headers;
}

chrome.downloads.onCreated.addListener(async (raw) => {
  const cfg = await getCfg();
  if (!cfg.enabled) return;

  const url = raw.finalUrl || raw.url || "";
  if (!/^https?:/i.test(url)) return;         // blob: / data: / file: 交给浏览器

  let host = "";
  try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return; }
  const block = (cfg.blockList || "").split(/\r?\n/).map(s => s.trim().toLowerCase()).filter(Boolean);
  if (block.some(b => host === b || host.endsWith("." + b))) return;

  const { item, size } = await waitSize(raw.id);
  if (!item) return;
  if (size > 0 && size < cfg.minSizeMB * 1024 * 1024) return;
  if (size < 0 && !cfg.unknownTakeOver) return;

  const fileName = sanitize((item.filename || "").split(/[\\/]/).pop());
  const headers = await buildHeaders(cfg, item);

  let added = null;
  try {
    added = await post("/add", {
      url: item.finalUrl || item.url,
      out: fileName,
      headers,
      conns: cfg.conns,
      dir: cfg.dir || ""
    });
  } catch (e) {
    // 本地服务没开：什么都不做，浏览器继续自己下
    setBadge("!", "#c0392b");
    return;
  }

  // 本地引擎已接手，取消浏览器这条
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) {}
});

chrome.action.onClicked.addListener(async () => {
  const cfg = await getCfg();
  const next = !cfg.enabled;
  await chrome.storage.local.set({ enabled: next });
  setBadge(next ? "ON" : "OFF", next ? "#1e8e3e" : "#888888");
});

(async () => {
  const cfg = await getCfg();
  setBadge(cfg.enabled ? "ON" : "OFF", cfg.enabled ? "#1e8e3e" : "#888888");
})();
