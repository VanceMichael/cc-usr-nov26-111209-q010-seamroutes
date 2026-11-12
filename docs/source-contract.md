# 道路分片来源

GET /manifest 返回 version、整数网格尺寸及区域所需完整tile列表。GET /tiles/x_y 返回含相同version的GeoJSON FeatureCollection，coordinates是同一全局整数网格，无浮点和坐标转换。tile表示主体范围 [x*size,(x+1)*size]，缓冲带允许出现范围外点。清单版本和片版本都要核对，HTTP成功不能证明同代。

LineString.properties.sourceRoadId只在version内稳定，partKey区分同编号不同支路，direction为both/forward；forward点序有业务方向，不能当作both反向等价。cutStart/cutEnd标明因瓦片裁切而形成的端点；没有切边标志的端点为真实道路端点，不能因为坐标相等自动连线。拼接候选须同道路、同partKey、方向兼容、精确坐标相等，切边应能在相邻主体片得到互补证据。相同原子边的逆序在both下等价，先规范化相邻顶点边再识别缓冲带重复；跨越不同中间点的折线不能只凭端点合并。name等属性矛盾要保留来源并阻止相应分支确定闭合。

初始右片503；/control/phase?value=complete 可取右片；value=updated 换清单和两片。分片集合有限，不需要搜索未知瓦片。示例含一个同编号独立支路及缓冲带反向重复边。部分片可以产生诊断，区域可信结果须所有声明片齐备，真实端点和切边未配对原因应可区分。不要求路线规划、地图渲染或任意几何纠错。
