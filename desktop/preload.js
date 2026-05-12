const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('ki', {
  loadConfig: () => ipcRenderer.invoke('load-config'),
  saveConfig: (config) => ipcRenderer.invoke('save-config', config),
  getEnv: (key) => ipcRenderer.invoke('get-env', key),
  setBgOpacity: (value) => ipcRenderer.invoke('set-bg-opacity', value),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  runDeploy: (onOutput, onDone) => {
    ipcRenderer.removeAllListeners('deploy-output')
    ipcRenderer.removeAllListeners('deploy-done')
    ipcRenderer.on('deploy-output', (_, text) => onOutput(text))
    ipcRenderer.on('deploy-done', (_, code) => {
      ipcRenderer.removeAllListeners('deploy-output')
      ipcRenderer.removeAllListeners('deploy-done')
      onDone(code)
    })
    ipcRenderer.send('run-deploy')
  },
})
