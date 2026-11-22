# IPv4 UDP 多播通信工具

在一台 Windows 电脑上运行三个独立节点。任一获准节点可以充当发送源，直接向房间组播地址发一次，在线组员各自从自己的 UDP 套接字接收、验证和播放。每个浏览器只连接本节点的 HTTP/SSE 服务。房主签发授权和处理成员变更，业务数据由各节点直接组播。

## 按需新增成员

配置好项目的 `.venv` 后，在 PowerShell 中每执行一次 `member.ps1`，就启动一个新节点，并在 Chrome 或 Edge 中打开一个独立窗口。脚本启动完成后返回终端，可以直接再次运行。

```powershell
.\member.ps1
.\member.ps1 -Name '成员 B'
```

脚本自动选取可用的本机 HTTP 端口，并在 `.demo/members/` 下为每个节点保存独立身份、数据和日志。新窗口打开后，可在页面创建房间，或粘贴邀请加入已有房间。默认组播配置与 `demo.ps1` 相同。自定义会合组时，成员应使用相同的 `-Group` 和 `-UdpPort`。

终端会打印该节点的页面地址、日志位置和停止命令。停止节点可执行其中的 `.\member.ps1 -Stop <成员ID>`。需要主动退出房间时，先在页面点击「退出房间」。可用 `-NoBrowser` 只启动节点，或用 `-BrowserPath` 指定浏览器可执行文件。

## 启动三节点

在 PowerShell 中运行以下命令。首次安装需要能从 PyPI 下载依赖。

```powershell
Set-Location -LiteralPath 'D:\资料\knowledge\网络编程\多播聊天室'
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\demo.ps1
```

演示脚本启动三个独立进程和三个浏览器页面。

| 节点 | 本地页面 |
| --- | --- |
| A | [127.0.0.1:8761](http://127.0.0.1:8761/) |
| B | [127.0.0.1:8762](http://127.0.0.1:8762/) |
| C | [127.0.0.1:8763](http://127.0.0.1:8763/) |

脚本每次在 `.demo/` 下新建一组节点目录，并打印各自的日志路径。回到运行脚本的终端按 Enter 结束三个进程。若 PowerShell 执行策略阻止启动本地脚本，可以为这一次启动指定进程级策略。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1
```

1. 在 A 点击「创建房间」和「复制邀请」。在 B 粘贴邀请并申请，在 A 批准，等两页显示房间已就绪。
2. 在 C 粘贴邀请，申请加入。候审期间从成员面板展开「组外联系」并发公开留言。A、B 都会在「组外留言」看到它。此时从 A 发一条组内文字，B 会收到，C 页面仍等待批准，标题栏只显示会合组。
3. 在 A 批准 C。等三页显示房间已就绪，从 A 发中文和 Emoji，B、C 各显示一条并标明发送者和时间。
4. 从 B 发送 [WAV 样例](samples/multicast-tone.wav) 或 [WebM 样例](samples/multicast-motion.webm)，A、C 可以各自下载、播放和拖动进度。启动脚本时加 `-FaultDemo`，再从 A 发送以观察组播补传。
5. 若用下节的独立进程方式启动，停止 A。等 B、C 把 A 标为离线，再从 B 给 C 发文字和文件。恢复 A 时使用原数据目录。A 恢复后会轮换房间密钥和数据组。
6. C 主动退出时，A、B 会更新列表。直接中断 C 进程时，其他节点在约 5 秒后将 C 标为离线。A 也可点击「移除 C」；轮换完成后 A、B 继续互通，C 要重新申请并由房主批准。

## 单独启动节点与配置

在三个 PowerShell 窗口中分别执行一行，可控制节点停止和恢复的时间。同一数据目录同时由一个进程使用。

```powershell
.\.venv\Scripts\python.exe -m multicast_chat --name A --http-port 8761 --data-dir .demo/manual/A
.\.venv\Scripts\python.exe -m multicast_chat --name B --http-port 8762 --data-dir .demo/manual/B
.\.venv\Scripts\python.exe -m multicast_chat --name C --http-port 8763 --data-dir .demo/manual/C
```

HTTP 监听在 `127.0.0.1`。所有节点使用相同的会合组和 UDP 端口，当前房间的数据组地址显示在每个获准节点的标题栏。接口参数填写当前节点的本地 IPv4 网卡地址。

| Python 参数 | 演示脚本参数 | 默认 |
| --- | --- | --- |
| `--group` | `-Group` | 会合组 `239.255.42.99` |
| `--udp-port` | `-UdpPort` | `50420` |
| `--interface` | `-Interface` | `127.0.0.1` |
| `--ttl` | `-Ttl` | `1` |
| `--http-port` | `-HttpBase` | `8761`，脚本依次分配三个端口 |
| `--file-limit-mib` | `-FileLimitMiB` | `32` MiB，上传及接收各按本节点上限检查 |
| `--rate-kib` | `-RateKiB` | `320` KiB/s，限制每节点的初传和补传块速率 |
| `--fault-demo` | `-FaultDemo` | 关闭，脚本开启时在 A 注入故障 |

`-NoBrowser` 只启动节点。用 `-FaultDemo` 演示组播补传时，A 会按每八块逆序初传，跳过第 2 和第 17 号块，重复发送第 1 号块，并丢弃每次轮换后发给每个成员的第一份密钥封装。分片编号从 0 开始。节点日志中的 `MISSING` 和 `phase=repair` 分别对应缺块请求和补传。

## 会合组与房间数据组

| 组播组 | 报文 | 订阅方式 |
| --- | --- | --- |
| 会合组 | 版本信标、申请、按成员封装的密钥、组外公开留言和离场通知 | 节点启动时加入；组外留言由独立的仅发送套接字投递 |
| 房间数据组 | 加密成员名单、心跳、组内文字、文件清单、初传块、缺块请求、补传块和接收确认 | 获得房主授权及当前密钥后调用 `IP_ADD_MEMBERSHIP`；退出、撤权和换组调用 `IP_DROP_MEMBERSHIP` |

IPv4 组播的接收条件是订阅目的组，发送端可以在组外。这里用两个目的地址落实公开留言和组内数据的接收范围。房间组从 `239.255.0.0/16` 本地管理范围派生，跟随房间版本轮换，与会合组共用配置的 UDP 端口。地址用于分流，授权和机密性由验签与加密保障。另行订阅数据组或直接抓包的第三方只能看到数据组密文。参考 MBone 的会合地址与会话地址分离方式，本项目覆盖题图所列的文字、文件、成员和加密功能。 [RFC 1112](https://www.rfc-editor.org/rfc/rfc1112.html) · [RFC 2365](https://www.rfc-editor.org/rfc/rfc2365.html) · [MBone 会话公告 RFC 2974](https://www.rfc-editor.org/rfc/rfc2974.html)。

## 成员、密钥与文件

- v2 报文使用 88 字节头和 64 字节 Ed25519 签名，UDP 负载上限 1200 字节。邀请固定房主公钥和指纹。房主将当前版本的房间密钥以 RSA-OAEP/SHA-256 封装给已获准成员，封装绑定房间、版本、会话、挑战和申请记录。
- 数据组使用 AES-256-GCM。完整报文头用作关联数据；HKDF 按房间、版本、发送者和进程会话派生子密钥，递增序号构造 nonce。接收方检查身份、版本、时效、签名和密文，并用滑动窗口处理乱序与重放。
- 当前版本发布后，多数获准身份的近期签名心跳维持发送权限。三节点中 A 停止时，B、C 仍有两票，可以继续通信。成员失去多数心跳后须重新同步才能发送。加入、移除和恢复掉线节点的密钥同步仍由房主授权。
- 成员变更时，房主发布新版本并停用旧组。旧名单有 N 人时，至少有 ⌈N/2⌉ 个旧身份停止为旧版续投，剩余身份才不足以组成多数。房主依靠自己的本地状态和存留成员的新密钥确认检查这一条件，再等旧心跳的发送权限过期。确认消息缺失时，房间保持切换中。轮换中止进行中的文件传输。重新加入须重新批准。
- 文件清单和每个 1000 字节分片都发往同一个房间组播地址。各接收方独立重组和校验，缺块请求与补传仍发往该组。最长 UDP 文件块负载为 1188 字节。收齐后核对长度和 SHA-256，通过才提供本地下载。WAV 和 WebM 播放使用本节点的 HTTP Range 路由。

每节点一次加入一个房间，房间最多 16 名获准成员。数据目录保留身份和授权，已校验文件保存在 `files/`。升级时请一起停止旧进程再用新版启动，可以沿用原数据目录。当前实测边界为同一台 Windows 的三个独立节点。

## 自动化验证

在项目根目录依次执行。浏览器测试需要已安装 Chrome 或 Edge，亦可用 `--browser` 指定浏览器可执行文件。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -X utf8 -m tests.probe
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 -m tests.room_smoke
.\.venv\Scripts\python.exe -X utf8 -m tests.file_edges
.\.venv\Scripts\python.exe -X utf8 -m tests.browser_acceptance
```

浏览器测试通过 Playwright 操作三页，同时在多个目的组捕获真实 UDP 报文。它核验组外留言、候审者的组订阅、房主停机期间的成员直传、两个接收方收到相同初传和补传序号，以及撤权时的数据组切换和旧密钥拒绝。日志和截图写入 `.test-run/browser-*/`。本次实测见 [验收记录](验收记录.md)。
