# npm-riscv64：项目理解与推进计划

> 版本：v7（2026-11-18，Q-f 定案 + 补入测试与验证策略）｜ 状态：**设计待最终确认，尚未改任何代码**
> 口径来源：README + 现有文件通读 + 五轮需求澄清
> 变更记录：见 `.dsh/change-logs/`（本版对应「包名即身份与测试策略-2026-11-18.md」）

---

## 0. 决策台账（已定，实现以此为准）

### 0.1 第一批（D1–D10）

| ID | 问题 | 定案 |
|---|---|---|
| **D1** | 版本全集来源 | **版本目录即全集**：每个版本目录必须有 `.stat`；`patches/`、`commands/` 可有可无。`common` 不参与可构建枚举 |
| **D2** | 合并语义 | **同名文件整体替换**（文件级）；未同名者继承；撤销用 `patches/.disable` |
| **D3** | 命令片段命名 | **固定五名：`fetch`/`patch`/`build`/`test`/`publish`**；未知名即报错 |
| **D4** | `.stat` 格式 | **YAML**；`upstream > source > npm` 逐级降级 |
| **D5** | 发布时机 | build/test 在 PR；publish 在合入 main 后 |
| **D6** | 安全边界 | fork PR 不给 secrets；不用 `pull_request_target` 跑 PR 代码 |
| **D7** | 构建载体 | **qemu 容器，固定在 template 内** |
| **D8** | rolldown | 仅占位（**已决定删除该目录**，见 D20） |
| **D9** | `scripts/**` 变更 | 仅渲染自检 |
| **D10** | 矩阵限流 | `max-parallel: 3~4` |

### 0.2 第二批（D11–D17）

| ID | 原编号 | 定案 |
|---|---|---|
| **D11** | Q1 | **不允许** `common/.stat`（`common` 只有 `commands/`、`patches/`） |
| **D12** | Q2 | `test` 缺失 = **不合格**；`build` 与 `test` 均必需 |
| **D13** | Q3 | `publish` 同 fetch/patch 策略：由元数据生成，配方可覆盖（具体形态见 **D18**） |
| **D14** | Q4 | 首个真包 = 纯 C 的小原生模块 |
| **D15** | Q5 | `source` 按**失败**处理：源码必须能定位到具体 ref |
| **D16** | Q6 | main 侧**复用 PR artifact**，不重复构建 |
| **D17** | Q7 | **一条**工作流文件承载两种触发 |

### 0.3 第三批（D18–D21）

| ID | 问题 | 定案 |
|---|---|---|
| **D18** | **发布模型** | npm 多架构包的标准形态是**两类包**：**A = 平台二进制包**（含 `.node`），**B = 纯 JS 包**（通过 `optionalDependencies` 引用 A）。对 A、B 的改动**都体现在 patch 中**（含新增 A 包的文件）。publish 阶段可能需要**发布两个包**：新增/更新的 A，以及更新后的 B |
| **D19** | Q-new-2 | commit SHA 形式的 `ref` **不需要额外约束**，只要满足「不可变」即可（不要求等于某 tag 指向的提交） |
| **D20** | Q-new-3 | **删除** `packages/rolldown/` 占位目录（D12/D13 下它必然渲染失败，占位语义交给样例配方） |
| **D21** | Q-new-1 / Q-new-4 | 首包按草案建议：`bufferutil`（主选）／`utf-8-validate`（备选）；artifact retention = 90 天 |
| **D22** | Q-a | **清单的生成发生在 publish 阶段**。A 的清单**不预设由 patch 提供**：由 patch 给出、或由渲染器在 publish 阶段生成，两条路都合法，`generate_manifest: true` 即「生成兜底」开关。这与 fetch/patch/publish「配方未提供则生成」是同一原则 |
| **D23** | Q-b | B 包同理：`generate_manifest: true` 且配方未提供 `publish` 时，渲染器**基于已有 `package.json` 更新生成**（不是从零创建），其余字段原样保留 |
| **D24** | Q-c / Q-d / Q-e | 采纳草案建议：发布演练先 verdaccio 再真发；只有 A 无 B 合法；首包 `bufferutil`（需严格纯 C 时换 `node-expat`） |
| **D25** | Q-f | **包名即身份**：update 模式下清单 `name` 与 `.stat` 不一致一律**失败**，不覆盖、不补齐。理由：改名发出去的不是「更新」而是「另一个包」，原包使用者拿不到 riscv64 支持 —— 与「让开发者无感获得 riscv64 支持」直接相悖。`version` 相反，由 `.stat` 决定并写入 |

### 0.4 规范（N1）

| ID | 规范 |
|---|---|
| **N1** | **国际化**：`README.md` 与全部代码（脚本注释、workflow 注释）一律**英文**；`.dsh/**` 不受约束 |

---

## 1. 项目定位

> Including patches, compile commands and so on for constructing riscv64 binaries for npm packages.

仓库是**配方库 + 构建编排**：配方描述「某个上游 npm 包在某个版本上，怎么取源码、打什么补丁、怎么编、怎么测、怎么发」，CI 按变更挑配方执行。

---

## 2. 现状快照

**初始快照（2026-11-18 阅读仓库时）**

| 文件 | 字节 | 角色 | Git 状态 | 语言合规 |
|---|---|---|---|---|
| `README.md` | 112 | 定位说明 | 已提交（`73e82d0`） | ✅ 英文 |
| `.github/workflows/template.yml` | 167 | 渲染模板 | 未跟踪 | ✅ 无注释 |
| `.github/workflows/pr-run.yml` | 1066 | PR 主工作流 | 未跟踪 | ✅ 无注释 |
| `scripts/render.py` | 1916 | 占位符渲染器 | 未跟踪 | ❌ L10、L28 中文 |
| `scripts/detect_targets.py` | 1388 | 变更包发现 | 未跟踪 | ❌ L12、L22 中文 |
| `packages/rolldown/` | — | 空占位目录 | 不跟踪 | — （**已删除**，D20） |

**当前快照（2026-11-18 第一轮实现后，详见 §13）**：模板迁到 `.github/templates/action.template.yml`；
两个旧工作流合并为 `.github/workflows/build.yml`；`scripts/` 三个脚本全量重写（英文注释）；
`packages/@ruyisdk/bufferutil/4.2.0/` 为首个真实配方；新增 `docker/`、`docs/`、`tests/`。
工作树内容仍未提交（`git ls-files` 只有 `README.md`），提交时机由你决定。

---

## 3. 配方契约

### 3.1 目录树

```
packages/
  <pkg>/                             # 无 scope，如 rolldown
    common/                          #   通用基线（不可构建；无 .stat）
      commands/                      #     可选：fetch/patch/build/test/publish
      patches/                       #     可选：*.patch + 可选 .disable
    <version>/                       #   可构建版本
      .stat                          #     必需
      commands/                      #     可选：同名覆盖 common
      patches/                       #     可选：同名覆盖 + 追加
  @scope/                            # scope 容器（本身不是包）
    <name>/
      common/
      <version>/
        .stat
        commands/
          publish                    #     覆盖生成的 publish
        patches/
          .disable
```

**包名 → 路径**：`<pkg>` 按 `/` 拆开落盘（`@scope/name` → `packages/@scope/name/`）。
**枚举**：`packages/*/` 不以 `@` 开头 = 无 scope 包；以 `@` 开头 = scope 容器，其子目录才是包。每个包下除 `common` 外的每个子目录 = 一个可构建版本。

### 3.2 合并语义（D2）

| 维度 | 规则 |
|---|---|
| 命令片段 | 版本目录 `commands/<name>` 存在 → **整体替换** common 的；否则继承 |
| 补丁 | 同名 → **整体替换**；不同名 → **追加**（common 组在前，组内按名排序） |
| 撤销 | `patches/.disable` 每行一个文件名，命中者不应用（common 与版本并集） |
| 点文件 | `patches/` 下点开头文件不算补丁 |
| 元数据 | `.stat` 只在版本目录出现（D11），不参与合并 |

### 3.3 `.stat` 元数据

```yaml
upstream:                          # 优先级 1
  repo: https://github.com/owner/repo
  ref: v1.0.0                    # tag 或 commit SHA（不可变，D15/D19）
  subdir: packages/binding       # 可选

source:                            # 优先级 2
  url: https://example.com/pkg-1.0.0.tar.gz   # 版本固定 URL
  sha256: <64-hex>                 # 必填（D15）

npm:                               # 优先级 3
  name: "@scope/binding"
  version: 1.0.0
  registry: https://registry.npmjs.org   # 可选

artifacts:
  path: "*.node"                   # build 产物（test 依赖）

# 发布清单（D18）：有序列表，可含 1~N 个包；binary 必须先于引用它的 main
npm_publish:
  - role: binary                   # A：平台二进制包（新增或更新）
    name: "@scope/binding-linux-riscv64-gnu"
    version: 1.0.0
    dir: packages/binding-linux-riscv64-gnu    # 相对源码根；可由 patch 创建
    registry: https://registry.npmjs.org
    token_env: NPM_TOKEN
    os: linux                      # 与 role=binary 配套；生成清单时写入
    cpu: riscv64
    generate_manifest: true        # true = 清单由 publish 阶段生成（缺则创建）；false = 必须由 patch 提供

  - role: main                     # B：纯 JS 包
    name: "@scope/binding"
    version: 1.0.0
    dir: .
    registry: https://registry.npmjs.org
    token_env: NPM_TOKEN
    requires: ["@scope/binding-linux-riscv64-gnu"]   # 决定发布顺序与一致性处理对象
    generate_manifest: true        # true = 基于已有 package.json 更新生成（补齐 requires 指向）
```

**清单生成时机与三档优先级（D22 + D23）**

```
① commands/publish 由配方提供  → 完全交给配方；generate_manifest 被忽略（渲染器不碰清单）
② 无配方片段 + generate_manifest: true  → 渲染器生成发布步骤，并在 publish 阶段生成/更新清单
③ 无配方片段 + generate_manifest: false → 渲染器生成发布步骤，但只校验清单，缺文件或不一致即失败
```

生成时的两种模式由**文件是否存在**自动判定（不需要额外字段）：

| 模式 | 触发 | 行为 |
|---|---|---|
| **create** | `dir` 下无 `package.json` | 用 `.stat` 生成最小清单：`name` / `version` / `main`（产物文件名）/ `os` / `cpu`（后两者仅 binary） |
| **update** | `dir` 下已有 `package.json` | **读入 → 只改需要的字段 → 写回**：`version` 对齐 `.stat`；`optionalDependencies[requires[0]] = A.version`。**其余字段原样保留** |

> update 必须是真正的 JSON 合并（`jq` / `node -e`），**不能 heredoc 整文件重写** —— B 包原有的 `dependencies`、`exports`、`files` 等字段一旦被覆盖，发出去的就是个残废包。

> **包名即身份（D25）**：update 模式下清单里的 `name` **必须**与 `.stat` 声明一致，不一致一律**失败** —— 不覆盖、不补齐、不跳过。改名发出去的不是「更新」而是一个**新包**：原包的使用者依然拿不到 riscv64 支持，还会多出一个无人引用的孤儿包，与「让开发者无感获得 riscv64 支持」的目标直接相悖。`version` 则相反，由 `.stat` 决定并**写入**（那是本次构建的目标版本，不是身份）。

**一致性的责任随档位转移**：

- 档位 ②（生成）：一致性由**写入**保证 —— 清单字段直接来自 `.stat`，事后无需比对。
- 档位 ③（不生成）：一致性靠**发前断言** —— 不一致即失败。

**ref / 完整性约束（D15 + D19）**

- `source.sha256` **必填**，缺失即硬失败。
- `upstream.ref` 与 `source.url` 中的 ref 必须**不可变**：拒绝 `main`/`master`/`develop`/`HEAD`/`latest` 等常见可变引用名；commit SHA 无需额外约束（不要求等于某 tag 指向的提交）。
- `npm` 途径天然版本固定（`name@version` → `dist.tarball`）。

**npm 途径的能力边界**：需 ① 通过 registry **元数据 API 解析 `dist.tarball`**（`GET https://registry.npmjs.org/<URL 编码后的 name>` → `versions[<ver>].dist.tarball` / `.integrity`），**不自行拼 URL**；② scoped 名 URL 编码（`@scope%2Fname`）。**局限**：registry 上是发布产物，原生模块包常只含预编译 `.node` + JS 包装层，**可能不含可编译源码** —— 故 npm 只作最后兜底，失败信息须提示这一原因。解包需 `--strip-components=1`。

### 3.4 结构 vs 数据

- **结构（模板持有）**：五步顺序、容器与 qemu、校验、发布门禁、YAML 形状。
- **数据（配方持有）**：`.stat` 元数据、补丁（**含 A/B 两包的改动**）、五个片段。
- **交界**：`fetch`/`patch`/`publish` 未提供同名片段时由渲染器生成（`publish` 的生成深度由 `generate_manifest` 决定，见 §6.4）；`build`/`test` 必须由配方提供（D12）。

---

## 4. target 模型与变更映射

### 4.1 target 标识

```json
[{"pkg":"rolldown","ver":"1.0.0","dir":"packages/rolldown/1.0.0"}]
[{"pkg":"@scope/binding","ver":"1.0.0","dir":"packages/@scope/binding/1.0.0"}]
```

**slug 规则**（job 名 / artifact 名 / 渲染目录共用）：`slug(pkg)` = 去前导 `@`、`/`→`-`；`slug(ver)` 原样。

| 用途 | 形态 |
|---|---|
| job 名 | `build (scope-binding @ 1.0.0)` |
| artifact 名 | `scope-binding-1.0.0-riscv64` |
| 渲染目录 | `.rendered/scope-binding/1.0.0/action.yml` |

**⚠️ YAML 引号规则**：`@` 是 YAML 保留指示符，标量以它开头即非法 → 模板中凡以变量开头的标量一律加引号（`name: "{{npm.name}}"`）。

### 4.2 变更 → 目标 映射

| 变更路径 | 展开为 |
|---|---|
| `packages/<pkg>/<ver>/**` | `<pkg>/<ver>` |
| `packages/@scope/<name>/<ver>/**` | `@scope/<name>/<ver>` |
| `packages/<pkg>/common/**`（含 scoped） | 该包**全部**版本 |
| 新增版本目录 | 该版本首次构建；缺 `.stat` → detect 阶段失败 |
| 仅删除版本目录 | 无目标，日志提示「已下线」 |
| `template.yml` | **全量** |
| `scripts/**` | **仅渲染自检**（D9） |
| 仅文档 | 无目标 |

**解析规则**：`packages/` 后首段以 `@` 开头 → 包名 = 段1/段2、版本 = 段3；否则包名 = 段1、版本 = 段2。版本段为 `common` 时按上表展开。

### 4.3 边界

- 同 PR 同时改 common 与版本目录 → 去重。
- 版本目录缺 `.stat` → 硬失败。
- `@scope` 容器目录下出现文件（而非子目录）→ 结构错误。
- **一个 (pkg, ver) 只产出一个构建目标**，但该目标可能需要发布 1~2 个包（A/B，D18）—— 构建维度不因 A/B 增加。

---

## 5. 触发与 CI 数据流

单文件、双触发（D17）：

```
① pull_request
   detect : git diff --name-only origin/$BASE_REF...$HEAD_SHA
   build  : fetch → patch → build → test          （publish 条件跳过）
   upload : artifact `<slug>-<ver>-riscv64`（名内含 head SHA，retention 90d）
   secrets: 不需要

② push: branches: [main]
   detect : git diff --name-only $BEFORE..$AFTER（before 全零 → 全量）
   reuse  : 定位 PR 侧产物并下载，失败/过期则回退重建
   publish: 按 `npm_publish` 清单发布（A → B，幂等）
   secrets: 需要
```

### 5.1 artifact 复用的定位链（D16）

```
merge_sha = github.sha
  → gh api repos/{owner}/{repo}/commits/{merge_sha}/pulls  → 已合并 PR 的 head SHA
  → gh run list --commit <head_sha> --workflow <name>      → run id
  → gh run download <run_id> -n <slug>-<ver>-riscv64
```

**双重回退（必须实现）**：

1. **定位失败** → 重建（直推 main、强推、PR 未跑过构建、超 90 天保留期）。
2. **有效性校验** → 产物来自 PR head 树，而发布用合并后树；若
   ```
   git diff <pr_head_sha>..<merge_sha> -- packages/<pkg>/ .github/workflows/template.yml
   ```
   非空 → 判为过期，重建。（两个 PR 先后合入时，后者产物可能基于前者合入**前**的 common 补丁。）

复用的产物同样要过 §6 的校验（含架构断言），不能默认可信。

---

## 6. 五步 composite action

### 6.1 五步 ↔ 五片段

| # | 步骤 | 片段 | 必需性 | 生成 |
|---|---|---|---|---|
| 1 | fetch upstream | `fetch` | 三途径至少一条可解析 | ✅ |
| 2 | apply patches | `patch` | 无补丁时整步省略 | ✅ |
| 3 | build | `build` | **必需**（D12） | ❌ |
| 4 | test | `test` | **必需**（D12） | ❌ |
| 5 | publish | `publish` | `npm_publish` 可生成 或 配方提供 | ✅（D18 多包） |

优先级：`commands/<name>`（版本）> `commands/<name>`（common）> 渲染器生成（fetch/patch/publish）。
`commands/` 下出现五名以外文件 → 渲染失败。

### 6.2 fetch（三途径降级）

```bash
if ! fetch_from_upstream; then
  if ! fetch_from_source; then
    fetch_from_npm || { echo "all source routes failed: upstream/source/npm" >&2; exit 1; }
  fi
fi
```

- `upstream`：tag/commit 两种情况分别生成 clone 参数；可选 `subdir` 作后续工作目录。
- `source`：`curl -fsSL` + `sha256sum -c` + `tar xz --strip-components=1`。
- `npm`：元数据 API → `dist.tarball` + `.integrity` → 下载校验 + 解包。
- 三途径全失败时**分别列出各自原因**。

### 6.3 patch

按 §3.2 组装名单 → 逐个 `git apply -p1 --3way`，失败即停并打印拒绝详情。

> 注意：A 包（平台二进制包）通常**由 patch 新增**（目录 + `package.json`），B 包的 patch 则改 `optionalDependencies` 加一行指向 A。因此 patch 必须能**创建新文件**（`git apply` 天然支持）—— 这一点要在 `docs/recipe-spec.md` 里写明。

### 6.4 publish（D13 + D18 + D22/D23：可能发两个包，清单可能由渲染器生成）

**顺序有实质意义**：**先 A 后 B**。若先发 B，则存在「B 已可安装但 A 尚不存在」的窗口，`npm install B` 在该窗口内会因 `optionalDependencies` 解析失败而退化；先发 A 可保证 B 一旦可见就一定可安装。

生成的 publish 步骤：

```bash
publish_one() {   # $1=dir $2=name $3=version $4=registry $5=token_env
  # ① 档位②：清单 create（缺文件）| update（JSON 合并，保留其余字段）
  # ② 档位③：断言 name/version/os/cpu 与 .stat 一致、requires 边一致
  # ③ 幂等：npm view <name>@<version> 命中 → 跳过
  # ④ 发布：npm publish <dir> --registry <registry> --access public
}

# 依赖序：role=binary 在前；再按 requires 建边拓扑排序
publish_one <A.dir> ... 
publish_one <B.dir> ...
```

**交叉校验的责任按档位分配（详见 §3.3 三档表）**：

| 校验 | 档位 ②（生成） | 档位 ③（不生成） |
|---|---|---|
| A 清单 **`name`** 与 `.stat` 一致（身份，D25） | **两档都做**：update 模式读入既有 `name` 比对，不一致即失败 | 必须断言 |
| A 清单 `version` 与 `.stat` 一致 | 写入即成立，无需事后再比 | 必须断言 |
| A 的 `os`/`cpu` 实际生效 | 写入 `os`/`cpu` 字段 | 必须断言 |
| 产物架构确实是 RISC-V（`file` 断言） | **仍必须断言**（生成清单不保证产物对） | **仍必须断言** |
| B 的 `optionalDependencies[A.name] == A.version` | 由 update 写入，**天然成立** | 必须断言，否则版本漂移静默通过 |

> 注意「写入即成立」不等于「不用校验」：清单由渲染器写，产物却是编译出来的 —— 架构断言两档都要做。

**时序陷阱：test 早于 publish**

清单更新发生在 publish 阶段，而 test 在更早的步骤运行。届时 B 的 `optionalDependencies` 尚未指向本次构建的 A —— 若 test 需要验证 A/B 联动，配方应在 `commands/test` 内自行把 A 目录链入（如 `npm install ./<A.dir>`），不能指望 publish 阶段的清单更新。

**部分失败可重入**：A 已发、B 失败 → 重跑时 A 命中间等跳过、只补 B。因此幂等检查不能省。

### 6.5 容器（D7）

模板内自持容器（写在模板里才能实现「改模板 → 全量重建」）：

```yaml
    - name: setup qemu
      uses: docker/setup-qemu-action@v3
    - name: <step>
      shell: bash
      run: |
        docker run --rm --platform linux/riscv64 \
          -v "$GITHUB_WORKSPACE:/w" -w /w -e NPM_TOKEN \
          ${RISCV_IMAGE} bash -lc '<片段内容>'
```

工作区 bind mount 共享 → 五步各自 `docker run --rm` 也能保留状态。

### 6.6 失败策略

| 步骤 | 失败处理 |
|---|---|
| fetch | 逐级降级；三条全失败 → 失败并列原因 |
| patch | 任一补丁失败即停 |
| build / test | 失败即停（不发布半成品） |
| publish | 校验失败即停；已发布者跳过；部分失败可重入 |
| 复用产物 | 定位失败 / 校验不过 → 回退重建（不算失败） |

---

## 7. 渲染器能力清单

| # | 能力 | 现状 | 优先级 |
|---|---|---|---|
| R1 | **缩进感知替换**（多行值按占位符列补齐前缀） | ❌ 纯 `re.sub`，多行必产出非法 YAML（已实测） | P0 |
| R2 | 两层叠加（同名整文件替换 + 补丁追加 + `.disable`） | ❌ | P0 |
| R3 | `.stat` YAML 解析（含 `npm_publish` 列表） | ❌ | P0 |
| R4 | 五片段固定名 + 未知名报错 + `build`/`test` 必需校验 | ❌ | P0 |
| R10 | **scoped 支持**：`@scope/name` 解析、slug、`@` 开头标量加引号 | ❌ | P0 |
| R5 | `fetch` 生成（三途径降级） | ❌ | P1 |
| R6 | `patch` 生成（排序、替换、`.disable`、`--3way`） | ❌ | P1 |
| **R7** | **多包 publish 生成**：三档优先级、清单 create / update(JSON 合并)、一致性（写入保证或发前断言）、依赖序拓扑、幂等、`--access public`、可重入 | ❌ | P1 |
| R8 | 必填校验：缺 `.stat` / 缺 `build` / 缺 `test` / `source` 无 `sha256` / 可变 ref / `npm_publish` 为空 / **清单 `name` 与 `.stat` 不一致（D25）** | ❌ | P1 |
| R9 | 可选步骤省略（无补丁时整步不渲染） | ❌ | P1 |
| R11 | `rstrip("\n")` 与块标量语义对齐 | ⚠️ | P1 |
| R12 | `--check-all` 渲染自检（D9） | ❌ | P1 |
| R13 | 事件模式适配（PR 三点 / push 区间、`before` 全零） | ❌ | P1 |
| R14 | 输出「目标 → 输入路径集合」供复用校验 diff | ❌ | P1 |
| R15 | `--warn-unused` | ❌ | P2 |

---

## 8. 与现状的差距

### P0

- **P0-1 渲染输入对不上**：`pr-run.yml:40` 传 `--input-dir inputs/<target>`，配方在 `packages/...` → `render.py:51-52` 直接 `SystemExit`；还需改为 `--pkg/--ver`。
- **P0-2 多行渲染出非法 YAML**（已实测）。
- **P0-3 target 未升级**：只取路径第 2 段；不产出 `{pkg, ver}`；不展开 common；不支持 push 区间；不支持 scoped。
- **P0-4 scoped 支持缺失**。

### P1

- **P1-1 五步骨架缺失**（模板只有 `echo`）。
- **P1-2 `.stat` 未实现**（含 D15 的 sha256/ref 约束）。
- **P1-3 无发布能力**：尤其**无 A/B 双包模型**（有序清单、清单 create/update、发布顺序、一致性处理、幂等、可重入）。
- **P1-4 无 riscv64 载体**。
- **P1-5 `template.yml` 位置**：会被当工作流解析（无 `on:`）→ 移至 `.github/templates/action.template.yml`，同步 `detect_targets.py:6`、`pr-run.yml:39`、渲染器默认值。
- **P1-6 artifact 复用缺失**（定位链 + 双重回退）。
- **P1-7 语言整改（N1）**：`render.py:10/28`、`detect_targets.py:12/22` 四处中文；新增代码一律英文。
- **P1-8 删除 `packages/rolldown/` 占位目录**（D20）。

### P2

矩阵命名与限流、产物架构断言、`.gitignore`（`.rendered/`、`__pycache__/`）、配方文档与示例（含「patch 如何新增 A 包」）、qemu 层缓存（`/root/.cargo`、`target/` 放宿主机路径）。

---

## 9. 阶段计划

| 阶段 | 内容 | 验收 | 状态 |
|---|---|---|---|
| **S0** | 样例配方（unscoped + scoped 各一）；`docs/recipe-spec.md`（英文）；删除 rolldown 占位 | 走查通过：`.stat` 三途径、`source` 带 sha256、common 一补丁 + 版本一补丁 + `.disable`、`build`/`test` 齐备、`npm_publish` 覆盖 **create（A）与 update（B）两种清单模式**；文档写明「patch 可新增 A 包」 | **完成（裁剪）**：真实配方只做 `@ruyisdk/bufferutil`（单包、upstream 一条路线、`generate_manifest: true`）；三途径降级、`source` sha256、common 补丁 + `.disable`、A/B 双包与 `verify` 档位改由 L0 夹具与 L2 假配方覆盖（理由见 §13.3） |
| **S1** | 渲染器 R1–R4、R8–R11 | 渲染样例 → `yaml.safe_load` 通过；多行缩进正确；scoped 合法（`@` 开头标量加引号）；缺 test/sha256 硬失败；`--check-all` 全绿 | **完成**：`--check-all` 全绿；渲染结果自动过 YAML 校验 + 结构断言；scoped 未加引号会被自己解析出来报错 |
| **S2** | detect 升级 R13 | 单测覆盖 §4.2 全表（含 scoped）；push（`before` 全零）场景 | **完成**：§4.2 全表 + scoped + 版本目录删除 + 全零 `before` 均有单测 |
| **S3** | fetch/patch 生成 + 五步端到端 | PR 跑通 `bufferutil`；upstream 失效 → 降级 source；三途径全失效 → 列出三条原因 | **完成（本机）**：L1 实跑 bufferutil 全五步（含发布到本地 registry）；降级链与「三途径全失败」由生成脚本保证，真 CI 复验见 L3 |
| **S4** | 容器 + 复用 + **A/B 双包发布** | qemu 固定进模板；artifact 复用（含过期判定）；发布顺序 A→B 可验证；update 不丢字段；漂移拦截；不重复发布；部分失败可重入 | **部分完成**：容器与 qemu 固定进模板 ✅；A→B 顺序、幂等、update 不丢字段、`verify` 档位拦截改名、部分失败可重入 ✅（L2）；**artifact 复用尚未接线**（只上传，不复用，留给 L3 一并调） |
| **S5** | 规模化：第二个包、限流、缓存、文档 | 多包多版本同 PR 变更矩阵正确；缓存命中可见 | **未开始** |

---

## 10. 开放项（已全部收敛）

| ID | 问题 | 结论 |
|---|---|---|
| **Q-a** | A 的清单由谁提供 | **D22**：不预设由 patch 提供。生成发生在 **publish 阶段**，由 `generate_manifest: true` 兜底；patch 直接提供亦可，两者不冲突 |
| **Q-b** | B 的 `optionalDependencies` 由谁改 | **D23**：同一原则。`generate_manifest: true` 且无配方 `publish` 时，渲染器**基于已有 `package.json` 更新生成**（只改需要的字段） |
| **Q-c** | 发布演练目标 | **D24**：先 verdaccio 演练，再对测试 scope 真发 |
| **Q-d** | 只有 A 没有 B | **D24**：合法，`npm_publish` 只列一项，不做 B 侧处理 |
| **Q-e** | 首包是否严格纯 C | **D24**：`bufferutil` 主选；需严格 C 则换 `node-expat`（C + libexpat） |
| **Q-f** | 清单已存在但 `name` 与 `.stat` 不一致（update 模式） | **已定（D25）：直接失败**。包名是身份，改名 = 发新包，使用者拿不到 riscv64 支持，违背「无感」原则 |

---

## 11. 待 CI 实测的技术风险

| ID | 风险 | 备选 |
|---|---|---|
| R-a | `docker/setup-qemu-action` 在托管 runner 上的 riscv64 支持 | 镜像内置 qemu-user-static + `--platform` |
| R-b | qemu 模拟性能（5–20×），Rust 包可能逼近 6h | 首包选纯 C（D14）；Rust 推迟到自建 runner |
| R-c | `dist.tarball` 指向 CDN/私有镜像 | `.stat` 用显式 `source.url` 覆盖 |
| R-d | artifact 被清理 / PR 未跑过构建 | §5.1 回退重建 |
| R-e | 复用产物基于 PR head 树而非合并树 | §5.1 有效性校验 diff |
| R-f | scoped 名的 `@` 在 YAML/路径/artifact 名中的特殊行为 | slug + 引号规则（R10） |
| **R-g** | **A/B 版本漂移**：B 的 `optionalDependencies` 指向 A 的旧版本 | 档位③发前断言拦截；档位②由 update 写入消除 |
| **R-h** | **发布顺序**：先发 B 会让「B 可装但 A 缺失」的窗口期存在 | §6.4 强制 A→B，并做幂等以支持重入 |
| **R-i** | **update 模式丢字段**：用 heredoc/整文件重写 B 的 `package.json` 会清掉 `dependencies`、`exports`、`files` 等 | §6.4 要求 JSON 合并（`jq`/`node -e`）；S4 验收比对非目标字段前后一致 |
| **R-j** | **test 早于 publish**：test 运行时 B 的 `optionalDependencies` 尚未指向本次 A，联动测试可能误判 | §6.4 时序说明；配方在 `commands/test` 内自链 A 目录 |
| **R-k** | 档位②「写入即成立」被误解为「无需校验」 | §6.4 明确：架构断言两档都做 |

---

## 12. 测试与验证策略

四层门禁，顺序 **L0 → L1 → L2 → L3**。L0–L2 全部可在本机闭环（能力已实测，见 §12.5），只有 L3 必须真 CI。

### 12.1 层次定义

| 层 | 覆盖对象 | 环境要求 | 能否替代 L3 |
|---|---|---|---|
| **L0 静态与单元** | 渲染器 R1–R4 / R7–R11、detect R13、生成片段语法 | 本机 Python + git，**零外部依赖** | 否 |
| **L1 片段真实执行** | fetch 三途径与降级、patch 应用、build 产物、架构断言 | 本机 docker + qemu binfmt + 网络 | 否 |
| **L2 发布语义** | 顺序 A→B、幂等、update 不丢字段、漂移拦截、可重入 | 本机 verdaccio（或 docker 版） | 否 |
| **L3 真 CI** | 见 §12.4 的 7 件事 | **GitHub Actions（需你提供仓库）** | — |

### 12.2 L0：静态与单元（可立即开始）

- **工具**：stdlib `unittest`（本机无 pytest；不为两个脚本引入依赖链，等需要夹具/参数化再考虑 pytest）。
- **夹具**：`tests/fixtures/<case>/`（配方输入）+ `tests/expected/<case>/action.yml`（golden）。
- **断言**：
  1. 渲染结果 `yaml.safe_load` 通过 —— **这是 P0-2「多行渲染出非法 YAML」的回归闸门**；
  2. steps 的数量/名称/顺序/`if:` 门禁（publish 在 PR 下被跳过）；
  3. 硬失败路径逐条：缺 `test`、缺 `.stat`、`source` 无 `sha256`、可变 ref、未知片段名、`npm_publish` 为空、清单 `name` 不一致（D25）；
  4. 叠加语义：同名整文件替换、补丁追加顺序、`.disable`、点文件排除；
  5. scoped：`@scope/name` 解析、slug、`@` 开头标量加引号、`.rendered/<slug>/<ver>/`；
  6. detect：临时 git 仓构造 PR 三点 diff 与 push 区间 diff（含 `before` 全零 → 全量）—— 此法已在现状脚本上验证可行；
  7. 生成的 shell 片段 `bash -n` 语法检查（shellcheck 本机缺失，可选后装）。
- **出口**：`--check-all`（R12）在全部夹具上全绿。

### 12.3 L1 / L2 明细

**L1（片段真实执行）**：从渲染出的 action.yml 中抽出每个 step 的 `run` 块，逐块执行

```bash
docker run --rm --platform linux/riscv64 -v "$PWD:/w" -w /w <image> bash -lc '<step run>'
```

用例：① upstream 正常取源（含 `subdir`）；② upstream 失效 → 降级 `source` 且 sha256 命中；③ 三途径全失效 → 失败信息含三条原因；④ 补丁正常应用 / 冲突时失败即停；⑤ build 产出 `.node` + `file` 断言为 RISC-V。

**L2（发布语义）**：本机起 verdaccio，构造最小上游树 + 两个包（A 用 create、B 用 update）。用例：① A 先于 B 发布；② 连跑两次，第二次全部命中等同跳过；③ update 前后 `package.json` 的**非目标字段逐字节不变**（R-i）；④ 手改 B 的 `optionalDependencies` 成旧版本 → 档位③失败（R-g）；⑤ A 成功、B 失败后重跑只补 B（可重入）。

### 12.4 L3：只能在真 CI 验证的 7 件事

1. `docker/setup-qemu-action@v3` 在**托管 runner** 上对 riscv64 是否就绪（R-a）。
2. job 名 / artifact 名含 scoped（`@`）与 semver（`.`, `-`, `+`）时 GitHub 是否接受。
3. `uses: ./.rendered/<slug>/<ver>` 这种**嵌套相对路径**本地 action 是否被接受（当前是假设）。
4. artifact 跨 run 复用：`commits/{sha}/pulls` → `gh run list --commit` → `gh run download`。
5. PR / main 双触发与 secrets 门禁（fork PR 无 secret）。
6. qemu 下的**真实耗时** vs 6h 上限（R-b；本机是 x86 CPU + 自有 binfmt，不代表 runner）。
7. `max-parallel` 实际并发与矩阵可读性。

### 12.5 本机能力实测（2026-11-18）

| 项 | 结果 |
|---|---|
| docker | ✅ 29.1.3（linux/x86_64, overlayfs） |
| qemu binfmt | ✅ `qemu-riscv64` 已注册（`/proc/sys/fs/binfmt_misc/qemu-riscv64`，flags `POCF`） |
| riscv64 容器 | ✅ 实测 `docker run --platform linux/riscv64 debian:trixie-slim uname -m` → `riscv64` |
| 网络 | ✅ `registry.npmjs.org` 200；`git ls-remote` GitHub 通 |
| jq / node / npm | ✅ jq 1.8.1 / node 22.22.1 / npm 9.2.0（L2 的 JSON 合并可本地验） |
| 缺失 | ❌ pytest ❌ gh ❌ shellcheck ❌ act（均非必须：unittest 替代、gh 仅 CI 需要、shellcheck/act 可选） |

结论：**L0–L2 可在本机闭环，L3 才需要 GitHub**。

### 12.6 需要你提供什么（最小清单）

| 项 | 需要吗 | 说明 |
|---|---|---|
| **私有 npm registry** | **不需要** | 演练用本地 verdaccio；端到端建议用 **GitHub Packages**（仓库自带 `npm.pkg.github.com`，只靠 `permissions: packages: write` + 内置 `GITHUB_TOKEN`，零额外维护），或公共 npm + 测试 scope + granular token。**只有**当你希望发布目标就是内部 registry 时，才需要它的地址 + token + 允许发布的 scope |
| GitHub 仓库（可跑 Actions） | **需要** | 建议**临时验证仓或 fork**，不要直接在 `RuyiRepo/npm-riscv64` 上推实验 workflow；稳定后再走 PR 合入主仓 |
| `NPM_TOKEN` | 视目标 | 选公共 npm 时需要（granular token，**限定测试 scope**）；选 GitHub Packages 时不需要 |
| riscv64 镜像来源 | 有更好 | Docker Hub 已可用；内网有加速/代理的话给地址 |
| 首包上游可达性 | 需要确认 | `bufferutil`（GitHub 可达）；若要求走内网镜像，给地址 |

### 12.7 排期建议

```
L0 ──▶ L1 ──▶ L2 ──▶ 交付一版可跑 workflow ──▶ L3（临时仓验证 7 项）──▶ 合入主仓
```

L0/L1/L2 不阻塞于环境申请，可以立即开始；L3 需要仓库就绪后才有意义。

---

## 13. 第一轮实现进展（2026-11-18）

### 13.1 交付物

| 路径 | 作用 |
|---|---|
| `scripts/recipe_lib.py` | 配方库：target 枚举、scoped 解析、`.stat` 校验、common/version 分层合并、patch 顺序与 `.disable`、仓库结构检查 |
| `scripts/render.py` | 渲染器：缩进感知替换、`{{#if}}/{{#unless}}` 块、`.stat` 展平与 `--override`、fetch/patch/publish 生成、YAML + 结构自校验、`--check-all` |
| `scripts/detect_targets.py` | 变更 → target：PR 三点 diff / push 两点 diff、common 展开、模板变更全量、`scripts/**` 自检、输出 `targets/has_targets/publish/self_check` |
| `.github/templates/action.template.yml` | 五步 action 模板；qemu + riscv64 容器固定在模板里（D7） |
| `.github/workflows/build.yml` | 单工作流双触发；detect → build（矩阵）→ 上传 artifact；发布由 `needs.detect.outputs.publish` 控制 |
| `packages/@ruyisdk/bufferutil/4.2.0/` | 首个真实配方：`.stat` + `commands/build` + `commands/test` |
| `docker/riscv64-toolchain.Dockerfile` | `debian:trixie-slim` + build-essential / nodejs / npm / python3 / node-gyp，`riscv64` 平台 |
| `docs/recipe-spec.md` | 配方规范（英文） |
| `tests/` | `test_render.py`、`test_detect.py`（L0）、`golden/`（参考配方的渲染快照，`regen_golden.py` 刷新）、`run_action_locally.py`（L1）、`l2_publish.py`（L2） |
| `README.md` / `.gitignore` | 英文总览；忽略 `.dsh/tmp`、`.rendered`、`__pycache__`、`node_modules` |

### 13.2 三层验证结果（本机实测）

| 层 | 命令 | 结果 |
|---|---|---|
| L0 | `python3 -m unittest discover -s tests` | **47 个用例全通过**（渲染器 + 检测器 + 生成脚本 `bash -n`/自洽 + **golden 快照** + 真实仓库 `--check-all` 集成），覆盖缩进/条件块/未解析占位符/分层覆盖/patch 顺序/`.disable`/scoped 引号陷阱/两档清单/仓库结构/§4.2 全表/全零 `before` |
| L1 | `python3 tests/run_action_locally.py --pkg @ruyisdk/bufferutil --ver 4.2.0` | **通过**：容器内 `uname -m` = `riscv64`；upstream 拉取 → `node-gyp rebuild` → 产物 `ELF 64-bit LSB shared object, UCB RISC-V, RVC, double-float ABI`；隐藏 `build/` 后 `node-gyp-build` 解析到 `prebuilds/linux-riscv64`，mask/unmask 往返一致 |
| L1+发布 | 同上 + `--publish`（`NPM_REGISTRY` 指向本地 verdaccio） | **通过**：`+ @ruyisdk/bufferutil@4.2.0`；tarball 内 `package.json` 已被改写成 `@ruyisdk/bufferutil@4.2.0` 且上游字段（`repository`/`scripts`/`dependencies`/`devDependencies` 等）逐字保留；包内 `prebuilds/linux-riscv64/bufferutil.node` 仍是 RISC-V ELF |
| L2 | `python3 tests/l2_publish.py` | **26 项断言全通过**：A→B 顺序、二次执行幂等、update 不丢字段、`verify` 档位拦截改名、部分失败可重入、新版本不影响旧版本 |

本机能力（2026-11-18 实测）：docker 29.1.3 + qemu riscv64 binfmt；`verdaccio/verdaccio:5` 镜像可拉起；node 22.22.1 / npm 9.2.0 / python 3.14.4（PyYAML 可用）；无 pytest/gh/shellcheck/act。

### 13.3 与计划文本的偏差（都是实现时被迫做的选择，需你确认）

| # | 偏差 | 原因 |
|---|---|---|
| 1 | 渲染产物不是「一个 action.yml 装所有 shell」，而是 `action.yml` + `steps/{fetch,patch,build,test,publish}.sh` | 把多行 shell 塞进 YAML 块标量后，(a) 无法在块内使用 heredoc（终止符必须顶格，而块标量要求缩进），(b) 单引号/反引号转义一旦出错就是运行期才炸。脚本落盘后 `bash -n` 可静态检查，模板只负责「怎么调」 |
| 2 | publish 步骤在**宿主**执行，不进 riscv64 容器 | 发布只是拷文件 + 打 registry，不需要模拟；容器里跑只会慢，且 token/`.npmrc` 的路径处理更绕。fetch/patch/build/test 仍在容器内 |
| 3 | 矩阵 job 里 `uses:` 用固定路径 `.rendered/action`（而不是按 slug 分目录） | GitHub 的 `uses:` **不接受表达式**；每个矩阵 job 有独立 workspace，固定路径天然隔离。本地 L1 仍用 `.dsh/tmp/l1/<slug>` 分目录 |
| 4 | 清单档位收敛为 **custom / generate / verify** 三档 | `create` 与 `rebrand` 在实现上是同一段逻辑（缺则建、有则合并更新），拆两档只会增加分支和文档噪声 |
| 5 | S0 的「unscoped + scoped 各一」裁剪为单一真实配方 `@ruyisdk/bufferutil` | 结构场景（三途径降级、`source`+sha256、common 补丁 + `.disable`、A/B 双包、`verify` 拦截）在 L0 夹具与 L2 假配方里能精确构造与断言；往真实配方里塞假补丁只会让首包变脏 |
| 6 | `.stat` 的 `npm_publish[].version` 必须等于版本目录名；仓库结构问题（空包目录 / 缺 `.stat` / 包目录散落文件）在 detect 阶段硬失败 | 版本目录即发布单位，「目录 4.2.0 里写 4.2.1」是必然的错误而不是风格选择 |

### 13.4 实测踩到的两个坑（已写进实现与文档）

1. **`npm publish src/.` 会被当成 GitHub 仓库简写**（`owner/repo` → `ssh://git@github.com/src/..git`）。生成脚本因此一律先 `cd` 到目标目录取绝对路径再发布；`npm view` 不受影响。
2. **npm 元数据会把 `optionalDependencies` 并入 `dependencies`**。L2 对「不丢字段」的断言改为「上游 `dependencies` 是发布后 `dependencies` 的子集」，并逐字比对 `description`/`scripts`/`files`。

### 13.5 L3 待验证清单（真 CI，需 fork 仓库）

| # | 要验证的点 | 若失败的对策 |
|---|---|---|
| 1 | `docker/setup-qemu-action@v3` 在托管 runner 上提供 riscv64（R-a） | 镜像内置 qemu-user-static，或用预构建的 `--platform` 镜像 |
| 2 | `uses: ./.rendered/action` 能在矩阵 job 里被解析（前一步刚生成） | 改为在 workflow 里直接 `bash .rendered/action/steps/*.sh`（牺牲一点「action 被消元」的纯度） |
| 3 | composite action 的步骤级 `if: inputs.publish == 'true'` 生效 | 改为在 job 级 `if` 控制，或拆一个 publish 专用 job |
| 4 | 发布到 GitHub Packages：npm scope 必须等于仓库 owner（`CannedFish/npm-riscv64` → `@cannedfish/*`） | 仓库变量 `PUBLISH_SCOPE=cannedfish`（渲染器 `--publish-scope` 会连同 `requires` 一起改写），配方保持 `@ruyisdk/*`；已连线 |
| 5 | `permissions: packages: write` + `GITHUB_TOKEN` 是否足够发布 | 退到 PAT（`secrets.NPM_TOKEN`），`token_env` 已支持任意变量名；首跑可用仓库变量 `SKIP_PUBLISH=true` 先只验构建 |
| 6 | qemu 模拟下 `bufferutil` 的真实耗时（R-b） | 纯 C 首包已是最省；Rust 包推迟到自建 runner |
| 7 | artifact 复用（S4 剩余）：`gh run download` 定位 PR 产物 + 过期判定 | 先只保留「失败即重建」的保守路径 |

### 13.6 常用命令

```bash
python3 -m unittest discover -s tests                     # L0
python3 scripts/render.py --check-all                      # 全量渲染自检
EVENT_NAME=pull_request BASE_REF=main python3 scripts/detect_targets.py
docker build -f docker/riscv64-toolchain.Dockerfile -t npm-riscv64/toolchain:riscv64 .
python3 tests/run_action_locally.py --pkg @ruyisdk/bufferutil --ver 4.2.0   # L1
python3 tests/l2_publish.py                                               # L2
```

L1/L2 的沙箱都在 `.dsh/tmp/` 下（已 gitignore），可随时删除重来。
