# 代码签名（还没签，这是申请清单）

## 为什么要签

不签名的 exe 会在别人机器上撞两堵墙：

1. **智能应用控制（SAC）直接拦死**：双击弹「已阻止 … 因为无法验证其发布者」，没有白名单、没有「仍要运行」，只能让对方关 SAC（关掉就降低整机防护，老系统关了就开不回来）。见 [Microsoft 的 SAC 常见问题](https://support.microsoft.com/zh-cn/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions)。
2. **SmartScreen**：第一次运行会弹「Windows 已保护你的电脑」，要等下载量和信誉攒起来才消。

SAC 见到**有效签名**就放行（不需要攒信誉），所以签名是对症的那一剂。

## 免费的路：SignPath Foundation

<https://signpath.io/> 的 Foundation 计划给开源项目免费签 Authenticode（证书由 SignPath Foundation 出）。它要求的和本仓库现状对照：

| 条件 | 本仓库 |
| --- | --- |
| OSI 认可的开源许可证、无商业双授权 | MIT ✅ |
| 不含闭源/专有代码 | ✅（`bin/desktopbeauty.exe` 是 `bin/launcher.cs` 编的，源码在库里） |
| 公开仓库、仍在维护 | ✅ <https://github.com/ambis01/desktop-beauty> |
| 已经有可下载的发布版 | ✅ v1.0.0 release |
| 签名的构建必须出自 CI | ✅ `.github/workflows/release.yml` |
| 申请人和仓库维护者是同一批人 | ✅ 只有你一个 |
| SignPath 和 GitHub 都开 MFA | ← **你要做的** |
| 每个版本签名前人工批准 | ← 后台点一下 |
| 首页写明「Free code signing provided by SignPath.io, certificate by SignPath Foundation」 | ← 通过后加到 README 底部 |

申请入口：<https://signpath.io/> 页面上的 **Apply for Free Code Signing**（`about.signpath.io/foundation`）。表里要填仓库地址、许可证、构建方式（GitHub Actions）、以及签名角色名单（Authors / Reviewers / Approvers，三个角色目前都可以是你）。

批准时间不固定，官方说明是人工审核 + 项目信誉评估，**别把它当成今天就能好的事**；等的时候就先用「让对方关 SAC」那条路。

## 批下来之后要接的

会拿到：组织 ID、项目 slug、签名策略 slug（一般 `release-signing`）、一个 CI 用户的 API token。

1. 仓库 → Settings → Secrets and variables → Actions：
   - Variables：`SIGNPATH_ORG_ID`、`SIGNPATH_PROJECT_SLUG`、`SIGNPATH_POLICY_SLUG`
   - Secret：`SIGNPATH_API_TOKEN`
2. 把 `.github/workflows/release.yml` 里「签名 / 换成签过名的文件」那两步的注释去掉（它们已经写在「打包 zip」之前，`github-artifact-id` 引用的是 `upload-unsigned` 那步）。**两个 exe 都要签**：转发器 `desktopbeauty.exe` 是入口，本体 `desktopbeauty-app.exe` 也要——签名必须在打 zip 之前，签完再压缩，解压出来的签名才有效。

```yaml
      - name: 签名
        uses: signpath/github-action-submit-signing-request@v1
        with:
          api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
          organization-id: ${{ vars.SIGNPATH_ORG_ID }}
          project-slug: ${{ vars.SIGNPATH_PROJECT_SLUG }}
          signing-policy-slug: ${{ vars.SIGNPATH_POLICY_SLUG }}
          github-artifact-id: ${{ steps.upload-unsigned.outputs.artifact-id }}
          wait-for-completion: true
          output-artifact-directory: dist-signpath
```

   `desktopbeauty.exe` 是仓库里已经编好的那份（`bin/launcher.cs` → `csc`），可以直接对着它签——签名只追加证书表、不改 PE 结构，签完照样能跑。真要重编就在 CI 里加一步 `csc /target:winexe /win32icon:assets\app.ico /out:bin\desktopbeauty.exe bin\launcher.cs`，再签。

## 花钱的路

- **Azure Artifact Signing**（原 Trusted Signing）：Basic 约 **$10/月**，个人可以做身份验证，签出来显示真实发布者名。适合「不想接 CI 审批、直接买」的情况。
- OV 证书（一年几百到一千多）+ `signtool` 本地签：老办法，证书要存在本机，CI 里得放密码，麻烦一些。

## 现在没签的情况下

发给别人时附一句：**这台电脑如果开了「智能应用控制」，要先用设置里关掉它**（设置 → 隐私和安全性 → Windows 安全中心 → 应用和浏览器控制 → 智能应用控制）。README 的「对方电脑弹『智能应用控制已阻止可能不安全的应用』」一节可以直接转给对方。
