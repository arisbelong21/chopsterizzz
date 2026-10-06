type DesktopBridge = {
  chooseOutputDirectory: (currentPath: string, callback: (selectedPath: string) => void) => void;
  setOutputDirectory: (path: string, callback: (saved: boolean) => void) => void;
  openOutputDirectory: (path: string, callback: (opened: boolean) => void) => void;
  getAcsApiKey: (callback: (apiKey: string) => void) => void;
  setAcsApiKey: (apiKey: string, callback: (saved: boolean) => void) => void;
};

declare global {
  interface Window {
    qt?: { webChannelTransport?: unknown };
    QWebChannel?: new (transport: unknown, callback: (channel: { objects: { chopsterDesktopBridge: DesktopBridge } }) => void) => void;
  }
}

function getDesktopBridge(): Promise<DesktopBridge | null> {
  if (!window.qt?.webChannelTransport || !window.QWebChannel) return Promise.resolve(null);
  return new Promise((resolve) => {
    new window.QWebChannel!(window.qt!.webChannelTransport, (channel) => {
      resolve(channel.objects.chopsterDesktopBridge || null);
    });
  });
}

export async function chooseOutputDirectory(currentPath: string): Promise<string | null> {
  const bridge = await getDesktopBridge();
  if (!bridge) return null;
  return new Promise((resolve) => bridge.chooseOutputDirectory(currentPath, resolve));
}

export async function setOutputDirectory(path: string): Promise<boolean> {
  const bridge = await getDesktopBridge();
  if (!bridge) return false;
  return new Promise((resolve) => bridge.setOutputDirectory(path, resolve));
}

export async function openOutputDirectory(path: string): Promise<boolean> {
  const bridge = await getDesktopBridge();
  if (!bridge) return false;
  return new Promise((resolve) => bridge.openOutputDirectory(path, resolve));
}

export async function getAcsApiKey(): Promise<string | null> {
  const bridge = await getDesktopBridge();
  if (!bridge) return null;
  return new Promise((resolve) => bridge.getAcsApiKey(resolve));
}

export async function setAcsApiKey(apiKey: string): Promise<boolean> {
  const bridge = await getDesktopBridge();
  if (!bridge) return false;
  return new Promise((resolve) => bridge.setAcsApiKey(apiKey, resolve));
}

export async function hasDesktopBridge(): Promise<boolean> {
  return Boolean(await getDesktopBridge());
}
