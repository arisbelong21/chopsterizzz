const PORTS = [18421, 18422, 18423, 18424, 18425];
const EXPECTED_PRODUCT_ID = "chopster-by-aris-v8.5.5";

async function localFetch(url, options) {
  const base = { cache: "no-store", credentials: "omit", ...options };
  try {
    return await fetch(url, { ...base, targetAddressSpace: "loopback" });
  } catch (err) {
    if (err && (err.name === "AbortError" || err.name === "TimeoutError")) throw err;
    return await fetch(url, base);
  }
}

async function probePort(port) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 900);
  try {
    const status = await localFetch(`http://127.0.0.1:${port}/status`, {
      method: "GET",
      signal: ctrl.signal,
    });
    if (!status.ok) return null;
    const identity = await status.json().catch(() => ({}));
    if (identity.product_id !== EXPECTED_PRODUCT_ID) return null;
    return identity;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function sendUrl(url) {
  let lastError = "Pastikan Chopster sudah dibuka";
  for (const port of PORTS) {
    try {
      const identity = await probePort(port);
      if (!identity) {
        lastError = "Pastikan Chopster sudah dibuka";
        continue;
      }
      const res = await localFetch(`http://127.0.0.1:${port}/add`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (!res.ok) { lastError = "Chopster menolak tautan"; continue; }
      const payload = await res.json().catch(() => ({}));
      if (payload.product_id !== EXPECTED_PRODUCT_ID) {
        lastError = "Port dipakai aplikasi lain";
        continue;
      }
      return { ok: true };
    } catch (err) {
      lastError = "Pastikan Chopster sudah dibuka";
    }
  }
  return { ok: false, error: lastError };
}

async function checkStatus() {
  for (const port of PORTS) {
    const identity = await probePort(port);
    if (identity) return { ok: true };
  }
  return { ok: false, error: "Pastikan Chopster sudah dibuka" };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "send-to-chopster",
    title: "Kirim ke Chopster",
    contexts: ["link", "page", "video"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl || (tab && tab.url);
  if (!url) return;
  await sendUrl(url);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "send") {
    sendUrl(msg.url).then(sendResponse);
    return true;
  }
  if (msg && msg.type === "status") {
    checkStatus().then(sendResponse);
    return true;
  }
  return false;
});
