# 麦当劳 MCP 实测契约

基于 2026-10 实测（`https://mcp.mcd.cn`，serverInfo `mcd-mcp 1.0.0`，工具 35 个）。
只记录**踩过的**和**每次都要用到的**，不是官方文档的抄写。

---

## 1. 接入

```
POST https://mcp.mcd.cn
Authorization: Bearer <TOKEN>
Content-Type: application/json
Accept: application/json, text/event-stream
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query-meals","arguments":{...}}}
```

- **无状态**：不用先 `initialize`，也不用回传 `Mcp-Session-Id`。直接 `tools/call` 就行。
- 可能回 `application/json`，也可能回 SSE；SSE 时取最后一条 `data:`。
- Token 获取：`open.mcd.cn/mcp` 控制台。本技能取 `MCD_MCP_TOKEN`，
  取不到就读 `~/.workbuddy/mcp.json` 里 `mcdonald` 连接器的 `Authorization`。
- 限流 600 次/分钟（`429`），`401` 是 token 失效。

## 2. 返回是「人读文本 + JSON + 可能再来一段人读文本」

```
# API Response Information
...字段说明（markdown）...
## Original Response
{"success":true,"code":200,"data":{...}}## 数据传参规则
1. 对于包含 unselectedKey 的特调组……
```

所以：

1. **不能 `json.loads` 整段**，尾部还有东西 → `Extra data`；
2. **不能从末尾倒着找 `{`**，说明文字里也有花括号；
3. 正确做法：定位 `{"success"`，用 `JSONDecoder().raw_decode()` 只解第一个完整对象
   （`scripts/mcd_client.py: find_json`）。

业务码不在 200 时抛错，`code` 是业务码；`60006 商品不可售` 最常见的原因是
**没带 `reservationDate`，查的不是同一份菜单**。

## 3. 单位：两套，必须分开

| 接口 | 单位 | 例子 |
| --- | --- | --- |
| `query-meals` | **元**（字符串） | `"currentPrice": "34.5"` |
| `calculate-price` | **分**（整数） | `"price": 9150` = ¥91.50 |

把菜单价除以 100 会得到 `¥0.14` 的麦乐鸡；把核价当元会得到 `¥9150` 的总价。
`menu._menu_yuan` / `menu._yuan` 分开处理，别再合并。

## 4. 菜单结构

```jsonc
data.categories[] = { name, meals: [{ code, tags? }] }   // 只给 code，名字在 meals 映射里
data.meals = { "<code>": { name, currentPrice, originalPrice, image, withOrder? } }
```

- 名字要从 `data.meals[code].name` 取，`categories` 里没有名字。
- 菜单**不按时段过滤干净**：晚上仍可能返回松饼、薯饼这类早餐品。
  非早餐时段要自己按关键词挡（`planner.BREAKFAST_ONLY`）。
- 会员卡类（`麦金卡`、`早餐卡`）也在菜单里，**不是吃的**，必须过滤。
- 同一家店同一时段，`categories` 里是策展分类（人气热卖可能混进马克杯、丝巾），
  所以「随便」不能取分类第一个，要按套餐 + 价格排序。

## 5. 套餐组成

```
data.rounds[] = { id, name, minQuantity, maxQuantity,
                  choices: [{ code, name, quantity, isDefault, diffPrice, supportModify, modification? }] }
```

- `diffPrice` 是**这个选项相对默认项的差价**（元）。
- 卡片上的「可以换」直接来自 `choices` 里非 `isDefault` 的项，不是我编的推荐。
- 下单/核价要带 `roundList` 时：

```jsonc
"roundList": [
  { "round": "1", "comboItemList": [{ "code": "<choice.code>", "quantity": 1 }] },
  { "round": "2", "comboItemList": [{ "code": "<choice.code>", "quantity": 1 }] }
]
```

不带 `roundList` 就用默认组合。**只有换过口味才需要带**，带的时候每个 round 都要给。
超集与子项数量以 `minQuantity/maxQuantity` 为准，别自己加。

### 特调（modification）

- 选中的项传 `selectedKey`，**未选中的项若 `unselectedKey` 不为空也必须传**；
- `minValues=1 / maxValues=1` 且只有一项时，该项必选，不能给「不选」；
- 套餐内的餐品也能特调，`choices[].modification` 里有结构。

本技能目前只用「套餐内换口味」这一层，特调留给单点排障。

## 6. 核价

```jsonc
data = { productOriginalPrice, productPrice, originalPrice, discount, price,
         productList: [{ productCode, productName, quantity, originalSubtotal, subtotal }],
         takeWayList: [{ code, title, subtitle }] }
```

- 全部是分。
- `price` 是应付；`fees = price - productPrice + discount` 是配送/打包/服务费合计
  （到店通常为 0，接口不单列字段）。
- `takeWayList[].code` 就是 `create-order` 要的 `takeWayCode`（到店：`eat-in` 堂食 / `locker-out` 外带）。
- `productList[].subtotal` 用来把整单金额摊到每个人头上：按 `subtotal / quantity` 得单价。

## 7. 门店

| 场景 | 工具 | 关键参数 |
| --- | --- | --- |
| 到店 | `query-nearby-stores` | `beType=1`、`searchType=2`、`city` **和** `keyword` 同时给 |
| 外送 | `delivery-query-addresses` → `delivery-query-stores` | `beType=2`、`addressId` |
| 团餐 | 同上 | `beType=6` |

- `city` 是业务城市，不是行政区。新区/开发区特别容易查空。
- 默认 `searchType=1` 是搜收藏，会返回空 → 必须显式传 `2`。
- 一次最多回 5 家，不是只有 5 家。
- `distance` 是「关键词解析点」到门店的直线距离，不是你的实际距离。
- 错误码：`600047` 周边无店（换 city）、`600050` searchType、`600057` 已打烊、`600058` city/keyword 缺一。

## 8. 预约

`reservationDate`（`yyyy-MM-dd HH:mm`）必须**四个接口用同一个值**：
`query-meals` / `query-meal-detail` / `calculate-price` / `create-order`。
漏一个，查到的就是另一份菜单，核价会报 `60006 商品不可售`。

门店返回的 `reservationTimeOptions[].reservationOptionText` 里是真实餐段名
（`早餐 / 午餐 / 下午茶 / 夜市 / 宵夜`）。注意晚餐在接口里叫**夜市**。

## 9. 营养表

`list-nutrition-foods` 的 `data` 是一个**字符串**，内容是 TOON 表：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
```

- 行数声明不可信，按实际行数解析。
- 表里是**单品**，套餐名对不上，要拆到子项再累加。
- 名字经常差一个杯型：`无糖可口可乐小杯` vs 表里的 `中杯` → 依次试原名/加杯型/换杯型。
- **0 kcal 是真实值**（无糖饮料），不能当成「没查到」。
- 查不到就不写热量；近似匹配用「≈」。

## 10. 下单

必填：`storeCode`、`orderType`、`beType`。
`orderType=1` 要 `takeWayCode`（来自核价）；`orderType=2` 要 `addressId`，团餐还要 `gmServiceCode`。
`remark` 只在 `orderType=2` 用，最多 50 字。`couponId` 放在 `items[]` 元素内部，不是顶层。

返回里有支付链接（`http...`）。**链接 15 分钟内有效。**

**额度**：`create-order` 每账号每日约 4 次，`cancel-order` 不退额度。
所以只有 `calculate-price` 可以随便调（免费），探索阶段绝不试单。

## 10.5 会写进用户账号的两个接口

下面两个是**写操作**，会留下真实痕迹，不是只读查询：

| 接口 | 后果 |
| --- | --- |
| `create-order` | 真实下单、真实扣款、订单记录进账号，且消耗当日额度 |
| `delivery-create-address` | **在本人麦当劳账号地址簿里新增一条地址**，不是临时地址 |

所以：**不要为了「试一下能不能送」去建地址**。地址只从 `delivery-query-addresses` 读；
用户主动要给新地址时，才调 `delivery-create-address`，并且先说清楚「这会存进你的地址簿」。

## 11. 缓存

只读工具（菜单、详情、门店、营养、时间）缓存 5 分钟，写操作永不缓存。
数据可疑或门店换过菜单时 `--no-cache` 清一次。
缓存目录在技能目录下 `.cache/`，可以随时删。
