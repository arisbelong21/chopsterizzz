type DesktopBridge = {
  chooseOutputDirectory: (currentPath: string, callback: (selectedPath: string) => void) => void;
  setOutputDirectory: (path: string, callback: (saved: boolean) => void) => void;
  openOutputDirectory: (path: string, callback: (opened: boolean) => void) => void;
  getAcsApiKey: (callback: (apiKey: string) => void) => void;
  setAcsApiKey: (apiKey: string, callback: (saved: boolean) => void) => void;
  listPublishTemplates: (callback: (json: string) => void) => void;
  publishViaTemplate: (templateId: string, callback: (json: string) => void) => void;
  publishStatusSnapshot: (callback: (json: string) => void) => void;
  publishStatus: { connect: (cb: (payload: string) => void) => void };
};

export interface PublishTemplate {
  id: string;
  name: string;
  summary: string;
  /** How each platform will publish, e.g. "youtube: API · tiktok: draft". */
  modes?: string;
}

export interface PublishResult {
  ok: boolean;
  message: string;
  run_id?: string;
}

export interface PublishSnapshot {
  running: boolean;
  stage: string;
  result: PublishResult | null;
  seq: number;
}

declare global {
  interface Window {
    qt?: { webChannelTransport?: unknown };
    QWebChannel?: new (transport: unknown, callback: (channel: { objects: { chopsterDesktopBridge: DesktopBridge } }) => void) => void;
  }
}

// A single QWebChannel must be created ONCE and reused. Creating a new channel
// per call (the old behaviour) made responses land on the wrong channel and
// threw "channel.execCallbacks[message.id] is not a function", leaving every
// bridge call pending forever (the ACS button stuck on "preparing").
let bridgePromise: Promise<DesktopBridge | null> | null = null;

function getDesktopBridge(): Promise<DesktopBridge | null> {
  if (!window.qt?.webChannelTransport || !window.QWebChannel) return Promise.resolve(null);
  if (!bridgePromise) {
    bridgePromise = new Promise((resolve) => {
      new window.QWebChannel!(window.qt!.webChannelTransport, (channel) => {
        resolve(channel.objects.chopsterDesktopBridge || null);
      });
    });
  }
  return bridgePromise;
}

/** Never let a bridge call hang the UI forever. */
function withTimeout<T>(promise: Promise<T>, ms: number, fallback: T): Promise<T> {
  return new Promise((resolve) => {
    let done = false;
    const timer = window.setTimeout(() => {
      if (!done) {
        done = true;
        resolve(fallback);
      }
    }, ms);
    promise
      .then((value) => {
        if (!done) {
          done = true;
          window.clearTimeout(timer);
          resolve(value);
        }
      })
      .catch(() => {
        if (!done) {
          done = true;
          window.clearTimeout(timer);
          resolve(fallback);
        }
      });
  });
}

export async function chooseOutputDirectory(currentPath: string): Promise<string | null> {
  const bridge = await getDesktopBridge();
  if (!bridge) return null;
  return withTimeout(new Promise<string>((resolve) => bridge.chooseOutputDirectory(currentPath, resolve)), 8000, '');
}

export async function setOutputDirectory(path: string): Promise<boolean> {
  const bridge = await getDesktopBridge();
  if (!bridge) return false;
  return withTimeout(new Promise<boolean>((resolve) => bridge.setOutputDirectory(path, resolve)), 8000, false);
}

export async function openOutputDirectory(path: string): Promise<boolean> {
  const bridge = await getDesktopBridge();
  if (!bridge) return false;
  return withTimeout(new Promise<boolean>((resolve) => bridge.openOutputDirectory(path, resolve)), 8000, false);
}

export async function getAcsApiKey(): Promise<string | null> {
  const bridge = await getDesktopBridge();
  if (!bridge) return null;
  return withTimeout(new Promise<string>((resolve) => bridge.getAcsApiKey(resolve)), 8000, '');
}

export async function setAcsApiKey(apiKey: string): Promise<boolean> {
  const bridge = await getDesktopBridge();
  if (!bridge) return false;
  return withTimeout(new Promise<boolean>((resolve) => bridge.setAcsApiKey(apiKey, resolve)), 8000, false);
}

export async function hasDesktopBridge(): Promise<boolean> {
  return Boolean(await getDesktopBridge());
}

export async function listPublishTemplates(): Promise<PublishTemplate[]> {
  const bridge = await getDesktopBridge();
  if (!bridge) return [];
  return withTimeout(
    new Promise<PublishTemplate[]>((resolve) => {
      bridge.listPublishTemplates((json) => {
        try {
          resolve(JSON.parse(json) as PublishTemplate[]);
        } catch {
          resolve([]);
        }
      });
    }),
    8000,
    [],
  );
}

export async function publishViaTemplate(templateId: string): Promise<PublishResult> {
  const bridge = await getDesktopBridge();
  if (!bridge) return { ok: false, message: 'Desktop bridge tidak tersedia.' };
  return withTimeout(
    new Promise<PublishResult>((resolve) => {
      bridge.publishViaTemplate(templateId, (json) => {
        try {
          resolve(JSON.parse(json) as PublishResult);
        } catch {
          resolve({ ok: false, message: json });
        }
      });
    }),
    8000,
    { ok: false, message: 'Desktop tidak merespons. Coba lagi.' },
  );
}

export async function publishStatusSnapshot(): Promise<PublishSnapshot | null> {
  const bridge = await getDesktopBridge();
  if (!bridge) return null;
  return withTimeout(
    new Promise<PublishSnapshot | null>((resolve) => {
      bridge.publishStatusSnapshot((json) => {
        try {
          resolve(JSON.parse(json) as PublishSnapshot);
        } catch {
          resolve(null);
        }
      });
    }),
    5000,
    null,
  );
}

/**
 * Subscribe to background publish progress + final result. The desktop sends
 * plain progress text first, then a final JSON payload ({"ok": ...}).
 * Returns an unsubscribe function.
 */
export async function subscribePublishStatus(cb: (payload: string) => void): Promise<() => void> {
  const bridge = await getDesktopBridge();
  if (!bridge || !bridge.publishStatus?.connect) return () => {};
  let disposed = false;
  const handler = (payload: string) => {
    if (!disposed) cb(payload);
  };
  bridge.publishStatus.connect(handler);
  return () => {
    disposed = true;
  };
}
