# seamroutes

配送道路分片 GeoJSON 的要素检查、HTTP 原文接收与区域接缝整理。

## 现有能力

inspect 查看一份源文件的结构；capture 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；show 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。

region 入口把一组同版本分片整理成可追溯的道路线记录：

- `receive <base_url>`：读清单、补齐声明片、做接缝检查，满足条件才发布区域结果。
- `diagnostics`：查看当前或指定代次的已取片状态、每次尝试和接缝结论；未发布也能看。
- `road <sourceRoadId>`：按道路编号查询发布版本里的来源片、缓冲重复和接缝解释。
- `demo-region`：用迟到邻片演示从 `missing_neighbor` 未闭合到可发布的全过程。

区域只使用清单声明的整数网格，`coordinates` 一律是二维整数，不做投影或坐标转换。

## 区域接收规则

- 一次接收代次绑定一份清单（version + 整数 `grid_size` + tile 列表）。清单换版会封存仍在接收中的旧代次；旧邻片绝不与新片拼接。
- 分片自报 version 必须与清单一致。HTTP 200 不等于同代：`version_mismatch` 会停止该代次并保留上一版发布结果。
- 只有声明片全部核验（`received`/`redundant`）且接缝检查无阻塞原因时，才写入 publications。
- 网络失败、503、重复下载和发布写库失败只记录尝试，不提前改变发布状态；旧发布版本继续可查。
- 每个输出段保留 `sources`（tile、feature_index、capture_id、原文摘要、方向、点区间）和 `duplicates`（被降级的缓冲副本及其规范代表）。

接缝只按来源事实处理：

- `sourceRoadId` 内不同 `partKey` 是不同支路，绝不因同编号连成一条线。
- `partKey` 相同且 `direction` 相同的切边端点，只有在**精确整数坐标相等**、且两个主体片在该点共享主体边线时才配对。
- `cutStart`/`cutEnd` 为 false 的端点是真实道路端点，坐标相等也不连接。
- 重复段先规范化相邻顶点原子边：both 下的逆序等价只降级为同一条段的重复证据；forward 的点序有业务方向，逆向不当作等价段。
- 非法/矛盾的 `name`、`direction` 等属性会保留来源并阻塞对应分支闭合。
- 未闭合原因区分 `missing_neighbor`（邻片未核验）、`unpaired_cut`（邻片已核验但无互补切边）、`ambiguous_seam`/`junction`（接缝不唯一或存在岔路）、`cut_off_boundary`、`forward_reversed`、`attribute_conflict`。区域声明边界的切边记为 `region_boundary_cut`，不制造邻片要求。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m seamroutes inspect seamroutes/fixtures/left.json
python3 -m seamroutes demo
python3 -m seamroutes demo-region
python3 -m unittest discover -s tests -v
python3 -m compileall -q seamroutes
```

需要手动发请求时运行 `python3 -m seamroutes.source_server --port 8765`，再在另一终端运行：

```sh
python3 -m seamroutes capture http://127.0.0.1:8765/tiles/0_0
python3 -m seamroutes receive http://127.0.0.1:8765
# 取到迟到邻片后再次 receive
python3 -m seamroutes diagnostics
python3 -m seamroutes road R7 --version roads1
```

默认会在当前目录创建 captures.sqlite3 和 objects；可通过 --db 和 --objects 指定独立位置。`receive --refetch` 会重新下载已核验的分片：摘要一致记为 `redundant`，失败则保留原核验状态与发布结果。演示自己启动源服务，使用随机本机端口，并在退出时清理临时目录。

## 模块

`seamroutes/parser.py` 负责单文档观察与分片严格解析，`geometry.py` 处理整数网格原子边与片邻接，`seams.py` 去缓冲重复并配对切边，`region.py` 编排接收代次、诊断与发布，`archive.py` 保存不可变原文、代次、尝试与发布记录，`transport.py` 限制实验请求到本机，`source_server.py` 提供来源示例。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t seamroutes:local .` 构建，再执行 `docker run --rm --network none seamroutes:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
