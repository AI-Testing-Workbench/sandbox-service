# TestAgent Cloud 沙盒服务

## 前置需求

1. 后台启动一个 OpenSandbox 服务，并不额外设置 API Key
2. 后台启动一个注册表 (容器仓库) 服务，可以参考下方的 compose 文件

```yaml
services:
  registry:
    image: registry:3
    container_name: registry-test
    restart: unless-stopped
    ports:
      - "5000:5000"

  registry-ui:
    image: joxit/docker-registry-ui:latest
    container_name: registry-test-ui
    restart: unless-stopped
    ports:
      - "5050:80"
    environment:
      - SINGLE_REGISTRY=true
      - REGISTRY_TITLE=Local Docker Registry
      - NGINX_PROXY_PASS_URL=http://registry-test:5000
      - DELETE_IMAGES=true
      - SHOW_CONTENT_DIGEST=true
```

3. 本地准备一个可以用于测试的 docker 镜像

4. 本地准备一个 FileBrowser Quantum，可以参考下方的 compose 文件和配置文件，注意需要预先在本地创建卷，并将 config.yaml 文件放置在卷中

compose 文件

```yaml
services:
  filebrowser:
    image: gtstef/filebrowser:1.5.6-stable

    user: "0:0"

    ports:
      - "8081:80"

    restart: unless-stopped

    volumes:
      - type: volume
        source: testagent-cloud-pvc
        target: /home/filebrowser/data
        volume:
          subpath: filebrowser

      - type: volume
        source: testagent-cloud-pvc
        target: /files

    environment:
      - "FILEBROWSER_CONFIG=data/config.yaml" # using our config file at ./data/config.yaml

    logging:
      driver: json-file
      options:
        max-size: "100m"
        max-file: "10"

volumes:
  testagent-cloud-pvc:
    external: true
```

配置文件
```yaml
server:
  minSearchLength: 3                      # 开始搜索所需的最小搜索关键词长度（默认：3）
  disableUpdateCheck: true                # 禁用后端更新检查服务
  numImageProcessors: 4                   # 用于生成预览图的并发图像处理任务数量，默认值为 4
  socket: ""                              # 要监听的 socket，例如 /var/run/filebrowser.sock
  tlsKey: ""                              # TLS 私钥文件路径
  tlsCert: ""                             # TLS 证书文件路径
  disablePreviews: true                   # 禁用所有缩略图预览，改用简单图标
  disablePreviewResize: false             # 禁用预览图尺寸调整，在慢速连接下可加快加载
  disableTypeDetectionByHeader: false     # 禁用通过文件头检测文件类型，文件系统较慢时可能有用
  port: 80                                # 服务监听端口
  listen: ""                              # 服务监听地址（默认：0.0.0.0）
  baseURL: "/"                            # 服务的基础 URL，即服务运行所在的子路径
  logging:
    - levels: ""                          # 要启用的日志级别列表，例如 "info|warning|error|debug"
      apiLevels: ""                       # API 要启用的日志级别，例如 "info|warning|error"
      output: "stdout"                    # 日志输出位置，例如 "stdout" 或 "path/to/file.log"
      noColors: false                     # 禁用日志颜色
      json: false                         # 使用 JSON 格式输出日志
      utc: false                          # 日志时间使用 UTC，而不是本地时间
      apiFilter: ""                       # 用于排除完整 API 路径日志的正则表达式。例如 '/user\?id\=self'
                                             # 默认：'^/health|^/favicon.ico|^/static|^/public/static'

  database: "data/database.db"                 # 数据库文件路径

  sources:                                # 数据源列表，必填
    - path: "/files"  # 文件系统路径，可以是相对路径或绝对路径，必填
      name: "根目录"                       # 数据源显示名称
      config:
        denyByDefault: false              # 默认拒绝访问，除非明确创建了 allow 访问规则
        private: false                    # 将数据源标记为私有，目前主要表示禁止共享
        readOnly: false                   # 只读数据源，UI、WebDAV 和 API 中的修改操作都会被禁止
        disabled: false                   # 禁用此数据源，可用于临时关闭而无需删除配置
        rules:                            # 针对指定路径应用的规则列表
          - neverWatchPath: ""            # 首次索引时包含该目录，但后续不再重新索引
            includeRootItem: ""           # 根目录层级只包含指定项目
            fileStartsWith: ""            # 全局排除以指定前缀开头的文件，例如 "archive-" 或 "backup-"
            folderStartsWith: ""          # 全局排除以指定前缀开头的目录
            fileEndsWith: ""              # 全局排除以指定后缀结尾的文件，例如 ".jpg" 或 ".txt"
            folderEndsWith: ""            # 全局排除以指定后缀结尾的目录，例如 ".thumbnails" 或 ".git"
            folderPath: ""                # 全局排除与指定路径匹配的目录
            filePath: ""                  # 全局排除与指定路径匹配的文件
            fileName: ""                  # 全局排除指定文件名，例如 "file.txt" 或 "test.csv"
            folderName: ""                # 全局排除指定目录名，例如 "folder" 或 "subfolder"
            viewable: false               # 允许在 UI 中查看，但不参与索引
            ignoreHidden: false           # 排除隐藏文件和隐藏目录
            ignoreZeroSizeFolders: false  # 排除大小为 0 的目录
            ignoreSymlinks: false         # 排除符号链接

        defaultUserScope: "/"             # 新用户默认访问范围，"/" 表示索引根目录，应与 path 下的目录对应
        defaultEnabled: true              # 是否默认将该数据源分配给新用户
        createUserDir: false              # 是否在 defaultUserScope + 用户名 下为每个用户创建目录
        useLogicalSize: false             # 使用逻辑大小而不是磁盘占用大小计算文件大小
                                          # 空目录会显示为 0 字节

  externalUrl: ""                         # 分享链接使用的外部 URL，例如 https://mydomain.com
  internalUrl: ""                         # 集成服务访问 FileBrowser 时使用的内部基础地址
                                          # 例如 http://localhost:8080

  cacheDir: "tmp"                         # 缓存目录，用于缩略图和其他缓存文件
  cacheDirCleanup: true                   # 是否自动清理缓存目录
                                          # 注意：Docker 中如果希望缓存持久化，也需要挂载持久卷
  maxArchiveSize: 5                       # 压缩/解压文件的最大大小，单位 GB
                                          # 0 表示无限制，默认 20

  filesystem:
    createFilePermission: "644"           # 新建文件的 Unix 权限，例如 644、755、2755，默认 644
    createDirectoryPermission: "755"      # 新建目录的 Unix 权限，例如 755、2755、1777，默认 755

  indexSqlConfig:                         # 索引数据库 SQL 配置
    batchSize: 1000                       # 单个事务中的批处理数量，通常建议 500-5000
                                          # 数值越大通常越快，但可能占用更多内存
    cacheSizeMB: 32                       # SQLite 缓存大小，单位 MB
    walMode: false                        # 启用 WAL 日志模式
                                          # 更复杂、更占内存，但持续有用户访问时并发性更好
    disableReuse: false                   # 启用后，每次启动都重新创建索引数据库
    startupIntegrityCheck: "quickCheck"   # 启动时检查索引数据库完整性的方式
                                          # 默认 quickCheck，可选 quickCheck、probe、off

  disableWebDAV: true                     # 禁用 WebDAV 支持，默认 false


auth:
  tokenExpirationHours: 144               # Web UI 登录 Token 的有效时间，单位小时，默认 2 小时

  methods:
    proxy:
      enabled: false                      # 是否启用代理认证
      adminGroup: ""                      # 如果设置，该组中的用户会获得管理员权限
      userGroups:                         # 如果设置，仅允许这些组中的用户登录
                                          # 其他用户即使凭证有效也会被阻止
      groupsClaim: ""                     # 用于读取用户组的 JSON 字段名，默认 "groups"
      userIdentifier: ""                  # 用作用户名的字段
                                          # OIDC 默认 preferred_username
                                          # JWT 默认 sub
                                          # 也可使用 email、username、phone 等
      disableVerifyTLS: true              # 禁用 TLS 校验，不安全，仅建议测试使用
      logoutRedirectUrl: ""               # 注销后跳转地址
                                          # 如果提供了服务商注销 URL，FileBrowser 也会重定向过去
      header: ""                          # 用于认证的 HTTP Header
                                          # 安全警告：FileBrowser 会直接信任该 Header 的值作为用户名

    noauth: false                         # 设为 true 后覆盖其他所有认证方式并完全关闭认证

    password:
      enabled: true                       # 是否启用密码认证
      minLength: 5                        # 密码最小长度，默认 5
      signup: false                       # 是否允许用户在登录页自行注册，不安全
      recaptcha:
        host: ""                          # reCAPTCHA 服务地址
        key: ""                           # reCAPTCHA Key
        secret: ""                        # reCAPTCHA Secret
      enforcedOtp: false                  # 是否强制所有密码用户使用 TOTP
                                          # false 时用户可以自行选择是否启用

    oidc:
      enabled: false                      # 是否启用 OIDC 认证
      adminGroup: ""                      # 该组中的用户获得管理员权限
      userGroups:                         # 仅允许指定组中的用户登录
      groupsClaim: "groups"               # 用于读取用户组的字段，默认 "groups"
      userIdentifier: "preferred_username" # 用作用户名的字段
      disableVerifyTLS: false             # 禁用 TLS 校验，不安全，仅建议测试使用
      logoutRedirectUrl: ""               # 注销后跳转地址
      clientId: ""                        # OIDC 应用 Client ID
      clientSecret: ""                    # OIDC 应用 Client Secret
      issuerUrl: ""                       # OIDC 提供商的授权地址
      scopes: "openid email profile"      # 请求的 OIDC Scope

    ldap:
      enabled: false                      # 是否启用 LDAP 认证
      adminGroup: ""                      # 该组中的用户获得管理员权限
      userGroups:                         # 仅允许指定组中的用户登录
      groupsClaim: ""                     # 用户组字段名称
      userIdentifier: ""                  # 用作用户名的字段
      disableVerifyTLS: false             # 禁用 TLS 校验，不安全，仅建议测试使用
      logoutRedirectUrl: ""               # 注销后跳转地址
      server: ""                          # LDAP 服务地址，格式 scheme://host:port
                                          # 例如 ldap://localhost:389
      baseDN: ""                          # LDAP 搜索基础 DN
                                          # 例如 dc=ldap,dc=goauthentik,dc=io
      userDN: ""                          # 服务账户 Bind DN
                                          # 例如 cn=admin,ou=users,dc=ldap,dc=goauthentik,dc=io
      userPassword: ""                    # 服务账户 Bind 密码
      userFilter: ""                      # 根据用户名查找用户的过滤表达式
                                          # 默认 (&(cn=%s)(objectClass=user))
                                         # 也可使用 (email=%s) 或 (sAMAccountName=%s)

    jwt:
      enabled: false                      # 是否启用 JWT 认证
      adminGroup: ""                      # 该组中的用户获得管理员权限
      userGroups:                         # 仅允许指定组中的用户登录
      groupsClaim: ""                     # 用户组字段名称
      userIdentifier: ""                  # 用作用户名的字段
      disableVerifyTLS: false             # 禁用 TLS 校验，不安全，仅建议测试使用
      logoutRedirectUrl: ""               # 注销后跳转地址
      header: ""                          # 用于读取 JWT Token 的 HTTP Header
                                          # 默认 X-JWT-Assertion
      secret: ""                          # 用于验证 JWT 签名的密钥
                                          # 可为 PUBLIC KEY、RSA PUBLIC KEY、
                                          # EC PUBLIC KEY 或 CERTIFICATE
      algorithm: ""                       # JWT 签名算法
                                          # 可选 HS256、HS384、HS512、RS256、ES256
                                          # 默认 HS256

    passkey:
      enabled: false                      # 是否启用 Passkey 多因素认证
      rpDisplayName: ""                   # Relying Party 显示名称
                                          # 默认使用 frontend.name
      rpId: ""                            # Relying Party ID
                                          # 留空时自动从基础 URL 的 Host 推导
      rpOrigins:                          # 允许的 Origin
                                          # 留空时自动从基础 URL 推导
      loginButtonText: ""                 # Passkey 登录按钮的自定义文字

  key: "8c7f4d2a9e61b3f05a47d8c2196e4fbb7a51c3902d8e6f14c3b97a25d0e8f461" # 用于签发 JWT Token 的密钥
                                          # 如果未设置，会自动生成随机密钥
  adminUsername: "admin"                  # 管理员用户名
                                          # 未设置时默认 admin
  adminPassword: "admin"                  # 管理员密码
                                          # 未设置时默认 admin
  totpSecret: ""                          # 用于加密 TOTP Secret 的密钥


frontend:
  name: "云端服务 PVC 管理"                 # 前端显示名称
                                          # PWA 安装名称最多 30 个字符
  disableDefaultLinks: false              # 禁用侧边栏中的默认链接
  disableUsedPercentage: false            # 禁用侧边栏中数据源的磁盘使用百分比显示

#  externalLinks:
#    - text: "(untracked)"                 # 链接显示文字
#      title: "untracked"                  # 鼠标悬停时显示的标题
#      url: "https://github.com/gtsteffaniak/filebrowser/releases/" # 链接地址
#
#    - text: "Help"                        # 链接显示文字
#      title: ""                           # 鼠标悬停时显示的标题
#      url: "help prompt"                  # 链接地址

  disableNavButtons: false                # 禁用侧边栏导航按钮

  styling:
    disableEventThemes: false             # 禁用基于事件自动切换的主题
    customCSS: ""                         # 自定义 CSS 文件路径
                                          # 例如 reduce-rounded-corners.css
    lightBackground: "#f5f5f5"            # 浅色模式背景颜色，可填写合法 CSS 颜色值
    darkBackground: "#141D24"             # 深色模式背景颜色，可填写合法 CSS 颜色值

    customThemes:                         # 用户可以选择的自定义 CSS 主题列表
                                          # 如果键名为 default，则作为默认主题
      alternative:
        description: "Reduce rounded corners" # 主题描述
        css: "reduce-rounded-corners.css" # 主题 CSS 文件路径
      default:
        description: "The default theme"  # 默认主题描述
        css: ""                           # 默认主题 CSS 文件路径

  favicon: ""                             # 前端 favicon 文件路径
  description: "FileBrowser Quantum is a file manager for the web which can be used to manage files on your server"
                                          # HTML head meta description 中使用的描述
  loginIcon: ""                           # 登录页面图标路径
  loginButtonText: ""                     # 登录按钮文字
  oidcLoginButtonText: ""                 # OIDC 登录按钮文字
  disablePWAInstall: true                 # 禁用侧边栏中的 PWA 安装按钮


userDefaults:
  sidebar:
    disableQuickToggles: false            # 禁用侧边栏快捷开关
    hideFileActions: false                # 隐藏侧边栏中的文件操作
    disableHideOnPreview: false           # 预览文件时保持侧边栏打开
    sticky: true                          # 页面导航时保持侧边栏打开
    hideFiles: false                      # 在侧边栏目录树中隐藏文件
                                          # true 时仅显示目录
    showTools: true                       # 显示分类为 tool 的侧边栏链接，默认 true

  listing:
    deleteWithoutConfirming: false        # 删除文件时不进行确认
    dateFormat: true                      # false 显示相对时间
                                          # true 显示准确时间戳
    showHidden: true                      # 在 UI 中显示隐藏文件
                                          # Windows 下也包括系统隐藏文件
    quickDownload: false                  # 显示一键下载按钮
    showSelectMultiple: false             # 桌面端显示多选功能
    singleClick: false                    # 单击打开目录
                                          # 同时允许使用鼠标中键在新标签页打开
    hideFileExt: ""                       # 在 UI 中隐藏的文件扩展名
                                          # 多个扩展名使用空格分隔
    showCopyPath: false                   # 在右键菜单中显示复制路径按钮
    deleteAfterArchive: false             # 成功创建/解压压缩包后删除源文件
    viewMode: "normal"                    # 查看模式，例如 normal、list、grid、compact
    gallerySize: 3                        # 图库缩略图大小，范围 0-9

  preview:
    image: true                           # 为图片文件生成缩略图
    video: true                           # 为视频文件生成缩略图
    audio: true                           # 为音频文件生成缩略图
    motionVideoPreview: true              # 鼠标悬停视频缩略图时显示多个视频帧
    office: true                          # 为 Office 文件生成缩略图
    popup: true                           # 鼠标悬停缩略图时显示更大的弹出预览
    disablePreviewExt: ""                 # 禁用预览的文件扩展名列表
                                          # 多个扩展名使用逗号分隔
    highQuality: true                     # 使用高质量预览缩略图
    folder: true                          # 如果目录中存在可预览内容，则为目录显示缩略图
    models: true                          # 为 3D 模型文件显示实时缩略图

  fileViewer:
    editorQuickSave: false                # 在编辑器中显示快速保存按钮
    autoplayMedia: false                  # 预览媒体文件时自动播放
    disableViewingExt: ""                 # 禁止直接查看的文件扩展名列表
    disableOnlyOfficeExt: ".md .txt .pdf .html .xml"
                                          # 禁止使用 OnlyOffice 编辑器打开的扩展名
    preferEditorForMarkdown: false        # Markdown 文件优先使用编辑器
                                          # 而不是 Markdown Viewer
    debugOffice: false                    # OnlyOffice 调试模式
    defaultMediaPlayer: false             # 使用浏览器默认媒体播放器
                                          # 而不是增强版媒体播放器

  search:
    disableOptions: false                 # 禁用搜索栏中的搜索选项

  ui:
    darkMode: true                        # 默认是否启用深色模式
    themeColor: "var(--blue)"             # UI 主题颜色
                                          # 例如 #ff0000、var(--red)、var(--purple)
    customTheme: ""                       # 从 customThemes 中选择的主题名称
    locale: "zh-CN"                       # 界面语言，例如 de、en、fr

  fileLoading:
    maxConcurrentUpload: 10               # 最大并发上传数量
    uploadChunkSizeMb: 10                 # 上传分片大小，单位 MB
    clearAll: false                       # 清除全部相关状态
    downloadChunkSizeMb: 0                # 下载分片大小，单位 MB

  account:
    permissions:
      api: true                           # 是否允许访问 API
      admin:  true                        # 是否允许管理员权限
      modify: true                        # 是否允许修改文件
      share: false                        # 是否允许分享文件
      realtime: true                      # 是否允许实时更新
      delete: true                        # 是否允许删除文件
      create: true                        # 是否允许创建或上传文件
      download: true                      # 是否允许下载文件

    lockPassword: false                   # 禁止用户修改自己的密码
    disableSettings: false                # 禁止用户访问设置页面
    loginMethod: "password"               # 登录方式，例如 password、proxy、oidc
    disableUpdateNotifications: true      # 禁用管理员用户的更新通知横幅


integrations:
  office:
    url: ""                               # OnlyOffice Document Server 地址
                                          # 必须能够被用户访问
    internalUrl: ""                       # FileBrowser 服务访问 OnlyOffice 的内部地址
                                          # 可以用于绕过反向代理
    secret: ""                            # OnlyOffice 集成认证密钥
    viewOnly: false                       # OnlyOffice 只读模式

  media:
    ffmpegPath: ""                        # ffmpeg 和 ffprobe 所在目录
                                          # 例如 /usr/local/bin

    convert:
      imagePreview:                       # 支持生成图片预览的格式
                                          # 默认值根据不同格式而有所不同
        heic: false
        jpeg: true

      videoPreview:                       # 支持生成视频预览的格式
                                          # 如果未明确禁用，默认通常为 true
        3g2: true
        3gp: true
        asf: true
        avi: true
        f4v: true
        flv: true
        m2ts: true
        m4v: true
        mkv: true
        mov: true
        mp4: true
        mpeg: true
        mpg: true
        ogv: true
        ts: true
        vob: true
        webm: true
        wmv: true

    debug: false                          # 输出 ffmpeg 标准输出用于调试
                                          # 注意：可能产生大量日志
    extractEmbeddedSubtitles: false       # 从媒体文件中提取内嵌字幕
    exiftoolPath: ""                      # exiftool 可执行文件路径


http:
  trustedHeaders:                         # 可信 HTTP Header 列表
                                          # 在反向代理之后运行时比较有用
  disableRateLimit: false                 # 关闭内置认证接口限流以及登录失败锁定
                                          # 默认 false
```

## 编译与使用

复制 `compose.yaml.example.example` 且重命名为 `compose.yaml`，
并根据需要修改配置

在项目根目录执行 `docker build -t sandbox-service:latest .` 构建镜像

在项目根目录执行 `docker save -o sandbox-service.tar sandbox-service:latest` 保存镜像

在项目根目录执行 `$in="sandbox-service.tar"; $out="$in.gz"; $src=[IO.File]::OpenRead($in); $dst=[IO.File]::Create($out); $gz=[IO.Compression.GZipStream]::new($dst,[IO.Compression.CompressionMode]::Compress); $src.CopyTo($gz); $gz.Dispose(); $dst.Dispose(); $src.Dispose() ` 压缩镜像

在项目根目录执行 `docker compose up -d`

如果没有修改默认的映射端口，则访问 `http://localhost:8080/docs` 查看 API 文档，
鉴权密码在 `compose.yaml` 中设置。

## 本地添加管理员账户

在容器内部运行 `python admin.py add <用户 ID>`。

## 约定的卷 (PVC) 的结构

```
/    # 根目录
    /filebrowser    # FileBrowser Quantum 持久化目录
    /filebrowser/config.yaml    # 上文中的配置文件
    /filebrowser/database.db    # 数据库文件

    /XXXXXX    # 用户ID
    /XXXXXX/YYYY-YYYY...    # 服务ID，无法直接使用
    /XXXXXX/YYYY-YYYY.../ZZZ-ZZZ...    # 容器ID，空文件，用于搜索等功能
    /XXXXXX/YYYY-YYYY.../...    # 其他文件夹，只挂载文件夹至容器中
```
