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
