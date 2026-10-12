// Application Electron de test : charge web_app.html dans une fenêtre affichée SANS activation.
// Usage : electron electron_main.js [--lab-x=X] [--lab-y=Y] [--lab-a11y] [--remote-debugging-port=N]
const { app, BrowserWindow } = require('electron');
const path = require('path');

const arg = (name, fallback) => {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.split('=')[1] : fallback;
};

app.whenReady().then(() => {
  if (process.argv.includes('--lab-a11y')) app.setAccessibilitySupportEnabled(true);
  const win = new BrowserWindow({
    x: Number(arg('lab-x', 140)),
    y: Number(arg('lab-y', 140)),
    width: 600,
    height: 640,
    show: false,
    title: 'Labo Electron',
    webPreferences: { contextIsolation: true },
  });
  win.loadFile(path.join(__dirname, 'web_app.html'));
  win.once('ready-to-show', () => {
    win.showInactive();
    win.moveBottom?.();
    process.stdout.write(JSON.stringify({ ready: true, pid: process.pid }) + '\n');
  });
  win.webContents.on('page-title-updated', (_e, title) => {
    process.stdout.write(JSON.stringify({ event: title.replace('Labo Web — ', '') }) + '\n');
  });
});
app.on('window-all-closed', () => app.quit());
