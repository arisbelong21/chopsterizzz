const PORTS = [18421, 18422, 18423, 18424, 18425];
const PRODUCT_ID = "chopster-by-aris-v8.5.5";
const statusEl = document.getElementById("status");

async function localFetch(url, options) {
  const base = { cache: "no-store", credentials: "omit", ...options };
  try {
    return await fetch(url, { ...base, targetAddressSpace: "loopback" });
  } catch (err) {
    if (err && (err.name === "AbortError" || err.name === "TimeoutError")) throw err;
    return await fetch(url, base);
  }
}

async function probe() {
  for (const port of PORTS) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 900);
    try {
      const r = await localFetch(`http://127.0.0.1:${port}/status`, {
        method: "GET",
        signal: ctrl.signal,
      });
      if (!r.ok) continue;
      const payload = await r.json().catch(() => ({}));
      if (payload.product_id === PRODUCT_ID) return { ok: true, port };
    } catch {
    } finally {
      clearTimeout(timer);
    }
  }
  return { ok: false };
}

async function checkStatus() {
  const found = await probe();
  if (found.ok) {
    statusEl.textContent = "Chopster terhubung.";
    statusEl.style.color = "#0F9F6E";
    return found;
  }
  try {
    const res = await chrome.runtime.sendMessage({ type: "status" });
    if (res && res.ok) {
      statusEl.textContent = "Chopster terhubung.";
      statusEl.style.color = "#0F9F6E";
      return { ok: true };
    }
  } catch {
  }
  statusEl.textContent = "Pastikan Chopster sudah dibuka.";
  statusEl.style.color = "#D64545";
  return { ok: false };
}

checkStatus();

document.getElementById("send").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) {
    statusEl.textContent = "Tidak ada tautan di tab ini.";
    return;
  }
  statusEl.textContent = "Mengirim...";
  statusEl.style.color = "#5B6B82";
  const found = await probe();
  if (found.ok) {
    try {
      const res = await localFetch(`http://127.0.0.1:${found.port}/add`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: tab.url }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && payload.product_id === PRODUCT_ID) {
        statusEl.textContent = "Terkirim ke Chopster.";
        statusEl.style.color = "#0F9F6E";
        return;
      }
    } catch {
    }
  }
  chrome.runtime.sendMessage({ type: "send", url: tab.url }, (res) => {
    if (res && res.ok) {
      statusEl.textContent = "Terkirim ke Chopster.";
      statusEl.style.color = "#0F9F6E";
    } else {
      statusEl.textContent = (res && res.error) || "Pastikan Chopster sudah dibuka.";
      statusEl.style.color = "#D64545";
    }
  });
});
