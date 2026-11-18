# 历史 ViaProxy 方案（2.0.0 起不再适用于 26.2 安装）

> **请优先阅读 [原生 26.2 安装说明](NATIVE_26_2.md)。以下保留为旧版历史文档，
> 其中“只支持 1.20.1”和“通过代理即可游玩”的说法不适用于新版，也不是已验证保证。**

## 旧版：在 26.2 / 其他版本上玩

本插件原生只实现 **Minecraft Java 1.20.1（协议 763）**。Minecraft **26.2** 是
2026-06-16 发布的版本，协议号 **776**（26.1 是 775，26.3 是 777）。两者之间隔着
1.20.2 的 configuration 阶段、1.20.5 的物品组件与 registry 同步、1.21.x 的多轮包
结构变化，以及 26.x 的区块/实体格式调整，**不是改一个版本号就能连上的**。

好消息是：**不用改插件，也能进 26.2**——用协议转换代理把 26.2 服务器"降"成
1.20.1 给插件，插件全程以为自己在玩 1.20.1。

## 先装 Java（"打不开 jar"就是这个原因）

`.jar` 不是压缩包，双击没反应通常是因为**没装 Java**，或者 `.jar` 被 WinRAR/7-Zip
关联了。ViaProxy 需要 **Java 17+（推荐 21）**。

1. 下载安装 **Java 21**（任选其一）：
   * [Adoptium Temurin 21](https://adoptium.net/temurin/releases/?version=21)
     （Windows 选 `.msi`，安装时勾选 **Add to PATH**）
   * [Microsoft Build of OpenJDK](https://learn.microsoft.com/java/openjdk/download)
2. 关掉并重新打开 PowerShell / 命令提示符，验证：

   ```bash
   java -version
   ```

   能看到 `openjdk version "21..."` 之类的输出就成功了。
3. 启动 ViaProxy（在 jar 所在目录）：

   ```bash
   java -jar ViaProxy.jar
   ```

   * 双击被解压软件打开：右键 jar → 打开方式 → **Java(TM) Platform SE binary**；
   * 提示 `Unable to access jarfile ViaProxy.jar`：当前目录没有这个文件。
     从 Releases 下载的文件通常叫 **`ViaProxy-3.4.14.jar`**（带版本号）。先 `dir *.jar`
     看真实文件名，然后 `java -jar .\ViaProxy-3.4.14.jar`；或把它重命名为
     `ViaProxy.jar`。路径有空格时用引号：`java -jar "D:\My Folder\ViaProxy.jar"`；
   * 提示 `Unsupported class file major version`：Java 太旧，装 21；
   * 提示 `Unable to access jarfile` 还有可能是没 `cd` 到 jar 所在目录，
     用 `pwd` / `cd` 确认。

### 一键脚本（推荐给不想敲命令的人）

插件自带 `tools/run_viaproxy.py`：

```bash
python tools/run_viaproxy.py
```

它会自动找 Java（PATH / JAVA_HOME / Windows 常见安装目录），没有就打开下载页；
然后从 GitHub 下载最新 ViaProxy（Java 8 环境会自动选 `+java8` 构建）并启动图形界面。
只想下载不启动：`--no-launch`；已有 jar：`--jar D:\ViaProxy\ViaProxy.jar`。

## 推荐：ViaProxy（本地代理，最省事）

[ViaProxy](https://github.com/ViaVersion/ViaProxy) 基于 ViaVersion / ViaBackwards，
支持 **1.7.2 – 26.3** 的客户端连接 **1.0 – 26.3** 的服务器，正好覆盖 26.2。

### 步骤

1. 安装 **Java 17+**（推荐 21），`java -version` 能输出版本号即可。
2. 从 [ViaProxy Releases](https://github.com/ViaVersion/ViaProxy/releases/latest)
   下载 `ViaProxy.jar`，放进一个空文件夹（它会在这里生成配置）。
3. 双击 jar，或在该文件夹执行：

   ```bash
   java -jar ViaProxy.jar
   ```

4. 在弹出的界面里填：
   * **Target Address**：你的 26.2 服务器地址，例如 `mc.example.com:25565`
   * **Target Version**：选 **26.2**（不是 1.20.1）
   * **Bind Address**：`0.0.0.0:25568`（默认就是 25568）
   * 正版服务器：先在 **Accounts** 页登录你的微软账号；离线服可跳过
5. 点 **Start**。界面会显示 `Listening on 0.0.0.0:25568`。
6. 在 MaiBot 插件配置里把服务器改成代理地址：

   ```toml
   [server]
   host = "127.0.0.1"
   port = 25568
   username = "MaiBot"
   auth = "offline"      # 正版登录交给 ViaProxy 的 Accounts，插件连本地代理用离线即可
   ```

7. `/mc connect`。插件日志里会看到服务器报告 26.2（协议 776），但会继续登录
   并提示"已通过协议转换代理进入 26.2 服务器"。

### 无头 / 服务器上跑（CLI 配置）

ViaProxy 支持先生成配置文件再启动：

```bash
java -jar ViaProxy.jar config viaproxy.yml   # 生成配置后退出
# 编辑 viaproxy.yml：bind-address / target-address / target-version
java -jar ViaProxy.jar start viaproxy.yml
```

> 不同版本的键名可能略有差异，**以生成的 `viaproxy.yml` 注释为准**；上面的键名
> 是该工具长期使用的命名。

## 备选：让服主装 ViaVersion + ViaBackwards

如果服务器是你或朋友开的，直接在服务端装
[ViaVersion](https://modrinth.com/plugin/viaversion) +
[ViaBackwards](https://modrinth.com/plugin/viabackwards)，1.20.1 客户端就能直接进
26.2 服务器，不需要任何本地代理，延迟也更低。插件配置仍然填服务器真实地址和端口。

## 需要注意

* **新内容不保证可用**：26.2 新增的方块/物品在 1.20.1 客户端里会被映射成旧方块或
  无法识别。插件能走动、挖旧方块、放旧方块、打怪、聊天，但别指望它理解 26.2 的
  新机制。
* **延迟与稳定性**：本地代理会多一跳；插件对移动是 20Hz 发包，代理会增加少量延迟。
* **正版验证**：正版服必须让 ViaProxy 持有一个正版账号（Accounts 页登录），插件
  连本地代理时用离线模式即可；ViaProxy 需要允许离线客户端接入（默认允许）。
* **反作弊**：部分服务器会检测代理或拒绝跨版本客户端。请只在允许的服务器使用。
* **不要改 `target-version` 为 1.20.1**：那样 ViaProxy 会去找 1.20.1 服务器；
  目标版本必须选你真正要进的 26.2。

## 为什么不做原生 26.2

* 协议层：1.20.2 起有 configuration 阶段与 registry data；1.20.5 起物品是
  data components；26.x 的区块 section 还多了 `fluidCount` 字段，块调色板与
  高度图格式也变了。每一条都需要重新实现并用真实服务端验证。
* 数据层：方块状态 ID、物品 ID、实体 ID 在每个版本都会重排；需要该版本的完整
  `minecraft-data`（目前上游只更新到 26.1，26.2 还没有完整协议数据）。
* 生态层：Mineflayer 官方支持列表目前到 26.1；26.2 仍依赖社区补丁。
  换言之，"用现成库绕过协议"这条路现在也走不通。

所以现阶段最稳的做法就是 **ViaProxy / 服务端 ViaVersion**，等上游数据齐全后再考虑
增加原生 26.x 后端（插件的行为层、寻路、人类化都可以复用，只需要替换协议客户端）。
