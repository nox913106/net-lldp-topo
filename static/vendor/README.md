# 第三方前端資源

這個目錄裡的檔案是連同程式一起送出去的，不是執行時才去外面抓。
拓樸圖需要 d3，而管理網段通常連不到網際網路 —— 從 CDN 載的話，
瀏覽器拿不到檔案，拓樸圖就會是一片空白而且不會報錯。

## d3-7.9.0.min.js

| | |
|---|---|
| 版本 | 7.9.0 |
| 授權 | ISC（見 `LICENSE.d3`） |
| 來源 | npm `d3@7.9.0`，`package/dist/d3.min.js` |
| 上游 | https://d3js.org |
| sha256 | `f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539` |

`tests/test_static_assets.py` 會檢查這個雜湊值，也會檢查沒有任何
介面檔案去外部網域取用資源。要換版本的話：

```bash
npm pack d3@<版本>                       # 取下 tarball
tar -xzf d3-<版本>.tgz                    # 解開
cp package/dist/d3.min.js static/vendor/d3-<版本>.min.js
cp package/LICENSE static/vendor/LICENSE.d3
sha256sum static/vendor/d3-<版本>.min.js   # 更新上表、README 與測試裡的雜湊值
```

然後把 `static/index.html` 裡的 `<script src>` 指向新檔名，並刪掉舊的。

## fonts/ — Chakra Petch 與 JetBrains Mono

介面主題（`static/css/theme-tactical.css`）用的字型，同樣隨程式送出，
不從 Google Fonts 抓。中文字沒有包含在內，退回系統的 Noto Sans TC。

| 檔案 | 來源（npm） | 授權 | sha256 |
|---|---|---|---|
| `chakra-petch-latin-600-normal.woff2` | `@fontsource/chakra-petch@5.3.0` | OFL 1.1（`fonts/LICENSE.chakra-petch`） | `a5888696e9eb1b4bbbecc8eb3922b8369f49d4bddb72263e033cbd17f399be76` |
| `chakra-petch-latin-700-normal.woff2` | `@fontsource/chakra-petch@5.3.0` | OFL 1.1 | `ce5095dc1cb200aaa939e38067a0677018d10e9f26ec38cdcf1557ac524fc775` |
| `jetbrains-mono-latin-400-normal.woff2` | `@fontsource/jetbrains-mono@5.3.0` | OFL 1.1（`fonts/LICENSE.jetbrains-mono`） | `14425ba9c695763c1547f48a206b7aa60350a33ae23de09f0407877f3fcd89eb` |
| `jetbrains-mono-latin-700-normal.woff2` | `@fontsource/jetbrains-mono@5.3.0` | OFL 1.1 | `d0d4e818808f2a0ba39b2b09d1989366f63494e295f003c7ef436697378507e8` |

取法：`npm pack @fontsource/<名稱>@<版本>`，解開後從 `package/files/` 複製，
`package/LICENSE` 存成 `fonts/LICENSE.<名稱>`。
