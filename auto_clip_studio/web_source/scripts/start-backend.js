import { spawn, spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webSourceDir = path.resolve(scriptDir, '..');
const projectRoot = path.resolve(webSourceDir, '..', '..');
const importRoot = path.dirname(projectRoot);
const probe = `import sys; sys.path.insert(0, ${JSON.stringify(importRoot)}); import fastapi, uvicorn; import chopster.auto_clip_studio.engine.main; print('OK')`;
const appCode = `import sys; sys.path.insert(0, ${JSON.stringify(importRoot)}); import uvicorn; uvicorn.run('chopster.auto_clip_studio.engine.main:app', host='127.0.0.1', port=8000, reload=True)`;

const candidates = [];
if (process.env.PYTHON_PATH) candidates.push({ command: process.env.PYTHON_PATH, args: [] });
for (const envName of ['.venv', 'venv', '.env']) {
  if (process.platform === 'win32') {
    candidates.push({
      command: path.join(projectRoot, envName, 'Scripts', 'python.exe'),
      args: [],
    });
  } else {
    candidates.push({
      command: path.join(projectRoot, envName, 'bin', 'python'),
      args: [],
    });
  }
}
if (process.platform === 'win32') candidates.push({ command: 'py', args: ['-3'] });
candidates.push({ command: 'python', args: [] }, { command: 'python3', args: [] });

let selected = null;
for (const candidate of candidates) {
  const check = spawnSync(candidate.command, [...candidate.args, '-c', probe], {
    cwd: projectRoot,
    encoding: 'utf8',
    timeout: 15000,
    shell: false,
  });
  if (check.status === 0 && check.stdout.includes('OK')) {
    selected = candidate;
    break;
  }
}

if (!selected) {
  console.error('No Python interpreter with FastAPI/Uvicorn and Chopster dependencies was found. Install requirements.txt or set PYTHON_PATH.');
  process.exit(1);
}

const env = {
  ...process.env,
  CHOPSTER_DEV_MODE: '1',
  PYTHONUNBUFFERED: '1',
  PYTHONPATH: [importRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
};
const backend = spawn(selected.command, [...selected.args, '-c', appCode], {
  cwd: projectRoot,
  env,
  stdio: 'inherit',
  shell: false,
});
backend.on('error', (error) => {
  console.error(`Failed to start Chopster Auto Clip Studio backend: ${error.message}`);
  process.exitCode = 1;
});
backend.on('close', (code, signal) => {
  if (signal) console.error(`Backend stopped by signal ${signal}`);
  process.exitCode = code ?? 1;
});
