# 构建 G4W Windows 便携包

Git 仓库只保存源码、脚本、文档和产品图片，不保存便携 Python、离线 wheels 或 `GenericAgent.exe`。

准备好 GenericAgent Desktop Portable 1.8 的 ZIP 或解压目录后，在仓库根目录运行：

```powershell
pwsh -NoProfile -File .\packaging\build_portable.ps1 `
  -BasePackage "D:\path\to\GenericAgent-Desktop-Portable-1.8.zip" `
  -WheelsDirectory "D:\path\to\G4W-complete-wheels" `
  -Version "1.0.0" `
  -CreateZip
```

生成结果：

```text
dist\G4W\
dist\G4W-1.0.0-win-x64.zip
dist\G4W-1.0.0-win-x64.zip.sha256
```

构建脚本会校验 `wheels.lock.json` 中的离线依赖，覆盖 G4W 源码和入口脚本，删除密钥、虚拟环境、缓存、用户记忆、向量模型和索引，并重新生成 `G4W_RELEASE_MANIFEST.json`。
