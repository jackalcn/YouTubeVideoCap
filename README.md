# YouTube影片下載平台

貼上 YouTube 影片、Shorts、播放清單或頻道連結，選擇畫質後下載影片、音訊與字幕。以 [Streamlit](https://streamlit.io) 和 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 建置。

請僅下載你擁有權利或已獲授權的內容（例如自己上傳的影片、Creative Commons 授權內容），並遵守 YouTube 服務條款與當地著作權法。

## 功能

| 頁面 | 可以做什麼 |
| --- | --- |
| **下載** | 貼連結或整段文字（自動找出其中的連結；影片 ID 和 @頻道代號要自己一行，可用空格或逗號分隔），或匯入清單檔。解析後可以預覽格式、大小與檔名，也能看字幕、章節、說明和匯出留言。播放清單和頻道可以勾選要下載的影片、匯出影片清單 CSV，也能排程在指定時間開始 |
| **佇列** | 任務在背景執行，切換頁面或關閉分頁都不會中斷。可暫停、續傳、取消、重試、調整順序與同時下載數，完成時可跳出桌面通知。可以設定下載時段；遇到 YouTube 限制會自動暫停再續跑，網路中斷會自動重試。App 重新啟動後，未完成的任務會接著做 |
| **搜尋** | 在 App 裡搜尋 YouTube，勾選後下載 |
| **媒體庫** | 瀏覽下載資料夾、依檔名搜尋、預覽影音與字幕、在單一字幕檔裡搜尋文字、開啟或刪除檔案（字幕、封面等附加檔一起刪，刪除會移到回收桶），以及清除未完成的暫存檔 |
| **訂閱** | 追蹤頻道或播放清單，定時下載新影片。可以編輯、從 Google Takeout 或 OPML 匯入，也可以設定保留規則，自動刪除舊影片 |
| **歷史** | 搜尋下載紀錄、重新下載、匯出 CSV |
| **工具** | 剪輯、轉成 H.264、壓縮、抽音訊、嵌入或燒錄字幕（可調字體大小、位置、背景與字型）、擷取畫面、音量標準化（-16 或 -23 LUFS）、合併多段影片（不同格式也能接，統一轉成 H.264）、轉成 GIF（最長 60 秒）。沒有字幕的影片可以用語音辨識產生字幕。字幕可以轉換格式、轉成台灣繁體，或合併成雙語字幕。有支援的顯示卡時可以加速轉檔 |
| **系統** | 檢查相依工具、更新 yt-dlp、檢查 App 新版本、測試連線、匯出匯入設定、備份與還原、通知、開機自動啟動 |

下載時可以設定：

- **影片**：畫質上限、編碼（H.264／VP9／AV1）、容器（MP4／MKV／WebM）、HDR、指定格式 ID、配音語言
- **音訊**：M4A、MP3、Opus、FLAC、WAV 或不轉檔，可設定位元率與封面。開啟「音樂標籤」時，會從標題整理出歌手與歌名，並把封面裁成正方形
- **字幕**：語言、格式、自動產生的字幕、嵌入影片、整理成純文字逐字稿。簡體字幕可以自動多存一份台灣繁體（含台灣用語）
- **片段與章節**：
  - 只下載指定時間或勾選的章節，或移除指定時間
  - 依章節分割，存進以影片命名的資料夾
  - 嵌入章節
  - 用 [SponsorBlock](https://sponsor.ajay.app) 標記或移除業配、自我宣傳、提醒訂閱等片段，章節名稱用中文
- **播放清單與頻道**：
  - 每個播放清單／頻道預設下載前 20 部，可在左側調整，本機最多 200 部；解析後的勾選清單最多列 500 部
  - 影片網址帶有 `list=` 參數時，預設只下載那一部影片
  - 勾選要下載的影片
  - 從清單尾端開始
  - 依片長、日期、觀看數、標題或授權篩選
  - 略過 Shorts 和已下載過的影片
- **直播**：錄製進行中的直播，隨時可以停止並保留已錄到的內容；等待還沒開始的直播（最多 24 小時）；另存直播聊天室紀錄
- **附加檔**：封面（可轉成 JPG）、影片資訊、說明、留言（CSV）、給 Kodi／Jellyfin／Plex 用的 .nfo。MKV 可以把影片資訊嵌入檔案
- **輸出**：
  - 檔名規則：不同影片同名時，會自動在檔名加上 `[影片 ID]`，不會互相覆蓋
  - 資料夾：依頻道或日期分，或使用媒體伺服器（Plex／Jellyfin）的資料夾格式
  - 自訂 yt-dlp 範本
  - 把檔案時間改成上傳日期
  - 優先使用繁體中文的標題與說明
- **網路**：限速、代理伺服器、影片之間的等待時間、YouTube 用戶端、只用 IPv4
- **進階**：直接加上 yt-dlp 的命令列參數

使用預設的 YouTube 用戶端時，若 YouTube 拒絕下載（HTTP 403），App 會自動改用 web_embedded、tv、tv_downgraded 用戶端再試一次；自己選了用戶端時不會更換。常用組合可以存成預設，也內建了音樂、Podcast、學習筆記等組合；在自己的電腦上，組合還可以一併記住下載資料夾，套用時直接切換。下載頁會顯示下載資料夾所在磁碟的剩餘空間，剩不到 2 GB 時仍可下載，但會提醒可能不夠。

## 在本機執行

需要 Python 3.11 以上和 [ffmpeg](https://ffmpeg.org)。沒有 ffmpeg 仍可下載，但畫質通常只有 360p，也無法轉成 MP3、剪輯片段或嵌入字幕。Windows 可以用 `winget install Gyan.FFmpeg` 安裝。

最簡單的方式：Windows 雙擊 `start.bat`；macOS 和 Linux 執行 `sh start.sh`。

- 第一次執行會自動安裝，之後直接開啟。
- 安裝中斷、Python 換了版本，或資料夾被移動時，再執行一次就會自動修好。
- 放在網路磁碟機上也能執行。

這兩個腳本預設只讓這台電腦連線；要開放給區網其他裝置，先設定環境變數 `VIDEOCAP_ADDRESS=0.0.0.0`，那些裝置會以受限權限連線（見下方「誰能做什麼」）。

也可以手動執行：

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m videocap --server.address localhost
```

macOS 和 Linux 請把路徑換成 `.venv/bin/python`。`python -m videocap` 會先啟動下載引擎，再啟動網頁伺服器；後面可以接任何 Streamlit 選項，例如 `--server.port 8600` 換埠號。手動執行時不會自動開瀏覽器，請自己開 http://localhost:8501（或加上 `--server.headless false`）。少了 `--server.address localhost`，Streamlit 會讓區網裡的所有裝置都連得到。`start.bat` 和 `start.sh` 後面也可以接同樣的選項。

### 開機自動啟動（Windows）

到「系統」頁開啟「開機自動啟動」，或執行 `autostart.bat`（取消：`autostart.bat remove`）。之後每次登入 Windows，App 會在背景啟動，不開視窗，直接用瀏覽器開 http://localhost:8501 即可。訂閱和排程下載要 App 在執行才會進行，所以常用訂閱的話建議開啟。

- 背景執行的這一份只接受本機連線，不受 `VIDEOCAP_ADDRESS` 影響。
- App 的路徑太長（超過 259 個字元）或含有 `%` 時，Windows 的捷徑會指到錯的地方，App 會拒絕建立並說明原因；請把 App 移到較短、沒有 `%` 的路徑。

背景執行的 App 要重新啟動時（例如更新 yt-dlp 之後），請登出 Windows 再登入，或在工作管理員結束 `pythonw.exe` 後執行 `start.bat`。

### 更新 yt-dlp

YouTube 常改版。如果下載開始失敗，先到「系統」頁按「檢查新版本」，有新版時再按「更新 yt-dlp」，或手動執行：

```
.venv\Scripts\python.exe -m pip install -U "yt-dlp[default,deno]"
```

yt-dlp 需要 [Deno](https://deno.com) 才能取得完整的畫質選項，上面的指令會一併安裝。App 偵測不到 Deno 時，會在左側提示，到「系統」頁按「安裝 Deno」即可。

### 更新 App

「系統」頁的「檢查 App 新版本」會向 GitHub 查詢；按過一次後，之後每天開系統頁時會自動再查。有新版時請重新下載 zip 或執行 `git pull`，再執行 `start.bat`（macOS／Linux 是 `start.sh`）。用 zip 下載、沒有 git 的版本只能用日期大略比對。

## 下載會員影片

用 App 左側的「會員登入」登入已加入會員的 YouTube 帳號，就能下載該頻道的會員專屬影片。YouTube 不開放第三方用帳號密碼登入，所以有兩種方式：

- **使用瀏覽器的登入狀態**：先在瀏覽器登入 YouTube，再於 App 選擇該瀏覽器。只在本機以 `localhost` 開啟時提供。Windows 上的 Chrome、Edge 常因加密或檔案鎖定而讀不到，建議使用 Firefox。
- **上傳 cookies.txt**：照 App 內「如何匯出 cookies.txt」的步驟匯出後上傳。

選好後可以按「確認登入狀態」先檢查。要下載整個頻道的會員影片，可以貼會員專屬清單：把頻道 ID 開頭的 `UC` 換成 `UUMO`，例如 `https://www.youtube.com/playlist?list=UUMOxxxxxxxx`。

請僅下載你已有權限且獲授權保存的內容。yt-dlp 官方提醒，帳號搭配 yt-dlp 使用有被暫時或永久封鎖的風險，請控制下載量。登入時任務會逐一執行，每部影片之間間隔 5–10 秒。

## 訂閱

在「訂閱」頁加入頻道或播放清單，App 會依設定的間隔檢查並下載新影片。加入時可以選「只追蹤之後的新影片」，不下載以前的影片。

- **編輯**：可以改名稱、檢查間隔、每次最多下載幾部、下載資料夾，或改用左側目前的下載設定。
- **匯入**：在 [Google Takeout](https://takeout.google.com) 只勾選 YouTube，匯出後找到 `subscriptions.csv` 上傳；也接受其他 App 匯出的 OPML。
- **保留規則**：可以只保留最新幾部，或刪除下載超過幾天的影片。只會刪這個訂閱自己下載、而且還在它資料夾裡的檔案，連同字幕等附加檔；刪掉的影片不會再被下載。

訂閱要 App 在執行才會檢查：保持 `start.bat` 開著、開啟「開機自動啟動」，或用 Docker 執行。

## 下載時段與自動處理

這些都只作用在自己電腦上的下載，公開主機訪客的任務不受影響：

- **下載時段**：在「佇列」頁設定，例如 02:00–07:00（可以跨午夜），用這台電腦的時間。時段外，排隊的下載和訂閱的自動檢查會等到時段開始才進行，正在下載的不會中斷，檔案工具也照常執行。預設關閉。
- **遇到 YouTube 限制自動暫停**：下載因為 HTTP 429 或「要求驗證」失敗時，整個佇列暫停 15 分鐘再自動繼續，避免帳號或 IP 被封得更久；有設定通知的話會收到一則訊息。按「立即繼續」或「全部繼續」可以提早結束；手動暫停的任務不會被喚醒；重新啟動 App 會清掉這個暫停。
- **網路中斷自動重試**：下載因為連線中斷失敗時，會在 2、5、10 分鐘後各自動重試一次，保留已下載的部分；按「重試」可以馬上再試。用上傳的 cookies.txt 登入的任務不會自動重試，因為任務停止後登入資料就不在了。

## 語音辨識字幕

沒有字幕的影片或音訊，可以用 [Whisper](https://github.com/openai/whisper)（faster-whisper）自動產生字幕。這是選用功能，只有在自己電腦上執行的擁有者看得到。

- **安裝**：到「系統」頁的「語音辨識」按「安裝語音辨識（下載約 125 MB）」，裝好不必重新啟動；也可以手動執行 `.venv\Scripts\python.exe -m pip install -r requirements-speech.txt`。
- **使用**：在「工具」頁選好影片或音訊，打開「語音辨識」分頁，選模型和語言後開始。字幕存成 SRT 放在原檔旁邊，例如 `影片.zh-TW.srt`；已有同名字幕時存成 `影片.zh-TW-ai.srt`，不會覆蓋。結果會列在「工具結果」，也會跟影片一起出現在「媒體庫」。
- **模型**：tiny（約 75 MB）、base（約 145 MB）、small（約 480 MB，只用處理器時建議）、medium（約 1.5 GB）、large-v3（約 3 GB）、large-v3-turbo（約 1.6 GB，準確度接近 large-v3 但快好幾倍，有顯示卡時建議）。越大越準，也越慢。每個模型第一次使用時會從 Hugging Face 下載到資料資料夾的 `models/whisper`（預設 `~/.youtube-videocap/models/whisper`），之後離線也能用；「系統」頁會列出已下載的模型。
- **語言**：自動偵測、中文、英文、日文、韓文。Whisper 常把中文寫成簡體字，預設會用 OpenCC 轉成台灣繁體和用語，夾在中文字之間的半形標點也會改成「，。？！」。選的語言和聽到的不同時，完成時會提醒。
- **速度**：只用處理器時，tiny 大約是即時的 9 倍（96 秒的語音約 11 秒）；1 小時的影片用 small 約需 20–40 分鐘，medium 可能和影片一樣長。
- **顯示卡**：有 NVIDIA 顯示卡時，「系統」頁會多出「顯示卡支援」的狀態，按「安裝顯示卡支援（下載約 650 MB）」裝好 cuBLAS 程式庫即可，不必另外裝 CUDA 或 cuDNN，裝好不必重新啟動（驅動程式要夠新）。之後「工具」頁的「使用顯示卡」會自動勾選。在 2016 年的 GTX 1050 Ti 上，tiny 模型處理 96 秒語音只要約 3.4 秒，模型越大差距越大。缺少程式庫時會自動改用處理器完成並提醒。
- **Docker**：在容器裡同樣用「系統」頁的按鈕安裝，套件和模型都存在 `/data`，重建容器也會保留。

## 刪除檔案

媒體庫的刪除和訂閱的保留規則，在 Windows 的本機硬碟、macOS 和 Linux 桌面上會把檔案移到回收桶，可以還原；網路磁碟、隨身碟、NAS、Docker 和伺服器沒有回收桶，會永久刪除，刪除前會先說明是哪一種。回收桶裡的檔案仍然佔用空間，保留規則是為了省空間的話，記得清空回收桶。「清除暫存檔」一律永久刪除。Windows 關閉了回收桶或檔案太大裝不下時，Windows 會直接永久刪除。

## 通知

在「系統」頁的「通知」填入 Discord Webhook 網址、Telegram Bot Token 與 Chat ID，或 [ntfy](https://ntfy.sh) 主題網址，下載完成或失敗、訂閱有新影片或檢查失敗時就會收到訊息。同一批下載只會發一則。存好的網址和 Token 不會再顯示在頁面上，也不會寫進備份。

只要瀏覽器開著 App，也可以在「佇列」頁開啟「完成時通知我」，任務完成時跳出桌面通知。瀏覽器只在 `localhost` 或 https 網址允許桌面通知，用區網 IP 或 Docker 的 http 網址開啟時不會有。

## 備份與還原

「系統」頁的「備份與還原」可以把訂閱、下載紀錄、預設組合、設定與下載紀錄檔打包成一個 zip，換電腦時再還原。備份不含通知的網址與 Token、代理伺服器的帳號密碼，以及進階 yt-dlp 參數裡的登入資料；還原時會保留這台電腦原本的這些設定。有任務在執行時不能還原。只還原你自己建立的備份：還原會帶回備份裡的所有設定，包括進階 yt-dlp 參數。

「匯入下載紀錄檔」可以匯入 yt-dlp 的 `--download-archive` 紀錄檔，之後這些影片會被當成已下載過而略過。

上傳的檔案最大 10 MB。備份檔超過時，可以用 `.venv\Scripts\python.exe -m videocap --server.address localhost --server.maxUploadSize 200` 啟動後再還原。

## 用 Docker 執行

適合放在家用伺服器或 NAS。檔案存在 `downloads/`，設定與紀錄存在 Docker volume。

```
docker compose up -d
```

- 容器一啟動就會開始檢查訂閱、接續未完成的任務，不必先開網頁。
- 預設只有這台電腦連得到（`http://localhost:8501`）。要讓其他裝置連線，請在 `docker-compose.yml`：
  - 拿掉 `127.0.0.1:`；
  - 設定 `VIDEOCAP_PASSWORD`；
  - 在 `VIDEOCAP_ALLOWED_HOSTS` 列出那些裝置在瀏覽器輸入的位址或名稱，例如 `192.168.1.20,nas.local`。

  沒列出的名稱只能用一般訪客的權限。
- 時區預設是 `Asia/Taipei`（`TZ`），用在伺服器端產生的時間，例如等待開播時顯示的預定時間。排程下載和頁面上的時間依瀏覽器所在的時區計算。
- 下載的檔案屬於 `downloads` 資料夾的擁有者；Docker 自動建立資料夾時是 uid 1000。要指定 NAS 帳號，請設定 `PUID` 和 `PGID`（用 `id` 指令查詢）。
- 映像檔已內建中文字型，燒錄的中文字幕不會變成方框。
- 在「系統」頁更新 yt-dlp 後，執行 `docker compose restart` 重新啟動。更新存在資料 volume 裡，`docker compose up -d` 重建容器也會保留；重新建置映像檔時會裝最新版。
- 容器先以 root 啟動，調整好資料夾權限後才切換成 `app` 使用者。在容器裡手動執行指令時，請加上 `-u app`，例如 `docker compose exec -u app videocap python -m pip list`，以免產生 root 擁有的檔案。

## 部署

線上版本：https://youtube-videocap.streamlit.app/

推送到 `main` 分支後，Streamlit Community Cloud 會自動部署；系統套件列在 `packages.txt`。

YouTube 常限制雲端主機的 IP。實測在 Streamlit Community Cloud 上，解析連結正常，但多數影片下載會被回 HTTP 403，所以要穩定下載請在本機執行。

也可以試著在 App 的「會員登入」上傳 `cookies.txt`，但這個做法尚未實測。cookies 等同登入憑證，請只上傳到你信任的部署環境。

### 誰能做什麼

App 會依連線來源決定權限，避免公開網址的訪客碰到主機：

| | 本機（localhost） | 公開主機或區網訪客 |
| --- | --- | --- |
| 下載到 | 自己選的資料夾 | 伺服器暫存，約兩小時後刪除，只能用瀏覽器存回去 |
| 任務 | 所有分頁共用 | 只看得到自己的。同時 1 項（同一個網路位址共用）；排隊時每人最多 5 項、同一個網路位址最多 10 項，整台主機最多 20 項。單項最長 30 分鐘，關閉頁面約 5 分鐘後自動取消 |
| 媒體庫、訂閱、歷史、檔案工具、詳細紀錄、備份、通知設定 | 有 | 無 |
| 代理伺服器、自訂範本、進階 yt-dlp 參數、選擇下載資料夾與資料夾分類（依頻道、播放清單、日期或媒體伺服器）、精準剪切、略過已下載過的影片、更新 yt-dlp、錄製直播、等待開播、聊天室紀錄、.nfo、讀取瀏覽器登入 | 有 | 無 |
| 標題篩選、字幕語言 | 可用正規表示式 | 只接受一般文字與語言代碼 |
| 一次解析的連結數／每部影片之間等待 | 100 個／最多 3600 秒 | 10 個／最多 10 秒 |
| 每個播放清單／頻道最多下載 | 200 部 | 20 部 |
| 另存留言 | 最多 100,000 則 | 最多 500 則 |
| 單檔／單次任務上限 | 無 | 1 GB／2 GB，伺服器上同時最多保留 3 GB 的完成檔案 |
| 清除自己的資料 | — | 「佇列」頁的「清除我的資料」會刪除自己的任務、暫存檔與上傳的 cookies |

存回電腦時要按兩次：先按「下載」準備檔案，再按「存到電腦」。多個檔案可以打包成 zip，但合計不能超過 50 MB；更大的請逐一下載。頁面上的時間都以瀏覽器所在的時區顯示。

App 透過 `localhost` 連線來判斷「本機」。如果在同一台電腦上用 nginx 等反向代理對外開放，代理必須帶上 `X-Forwarded-For` 標頭（nginx 的常見寫法是 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`），否則所有訪客看起來都像本機。保險起見，也可以直接設定 `VIDEOCAP_MODE=cloud` 和密碼。

### 存取密碼

設定 `VIDEOCAP_PASSWORD`，或在 `.streamlit/secrets.toml` 寫入 `app_password = "你的密碼"`，開啟 App 時就要先輸入密碼。

- `secrets.toml` 請存成 UTF-8（Windows 11 記事本的預設格式，含 BOM 也可以）。存成 UTF-16 或 Big5 時，App 會在啟動時停下來並說明原因；內容格式錯誤時，App 會保持鎖定。
- 同一個網路位址連續輸錯 5 次後，要等 30 秒才能再試，之後每次加倍，最長 5 分鐘。

### 環境變數

| 變數 | 用途 |
| --- | --- |
| `VIDEOCAP_PASSWORD` | 開啟存取密碼，見上方「存取密碼」 |
| `VIDEOCAP_MODE` | `local` 或 `cloud`，強制指定模式，取代自動偵測 |
| `VIDEOCAP_TRUST_REMOTE` | 設為 `1` 時把遠端連線視為本機擁有者，用於 Docker 等私人環境。請同時設定密碼和 `VIDEOCAP_ALLOWED_HOSTS` |
| `VIDEOCAP_ALLOWED_HOSTS` | 搭配 `VIDEOCAP_TRUST_REMOTE`：除了 localhost，還信任哪些網址名稱，以逗號分隔。可以防止其他網站冒充這台主機 |
| `VIDEOCAP_OUTPUT_DIR` | 預設下載資料夾，預設是 App 資料夾裡的 `downloads` |
| `VIDEOCAP_DATA_DIR` | 紀錄與設定的位置，預設是家目錄下的 `.youtube-videocap` |
| `VIDEOCAP_ADDRESS` | `start.bat`／`start.sh` 監聽的位址，預設 `localhost` |
| `VIDEOCAP_HEADLESS` | `start.bat`／`start.sh` 設為 `true` 時不自動開瀏覽器 |

### 從 YouTube 頁面直接送過來

在網址加上 `?url=連結` 會把連結填入下載頁，但不會自動開始。「工具」頁有現成的書籤小工具，在 YouTube 影片頁點一下就能送到 App。

手機也可以：在 iOS 的「捷徑」App 建立一個「開啟 URL」的捷徑，網址填 `http://電腦的位址:8501/?url=` 接上「捷徑輸入」，再從 YouTube App 的分享選單選它；Android 可用任何能把分享連結接到網址後面的工具。手機要連得到這台電腦（區網要設定 `VIDEOCAP_ADDRESS=0.0.0.0`，或用 Docker），而且會以訪客權限使用。

## 已知限制

- 下載片段時，yt-dlp 透過 ffmpeg 處理，過程中沒有進度百分比，取消也要等這一段處理完。
- 搜尋結果只有相關性排序。YouTube 的日期排序參數實測沒有作用，所以不提供。
- 續傳靠 yt-dlp 的 `.part` 檔；YouTube 的下載網址有時效，暫停太久可能要重新解析，App 會自動處理。
- 直播錄製從按下開始的時間點起算，無法回溯到直播開頭。
- 用瀏覽器存回電腦的單一檔案最大 2 GB；更大的檔案請直接到下載資料夾取用。
- 同一個資料與設定資料夾（預設是家目錄下的 `.youtube-videocap`，或 `VIDEOCAP_DATA_DIR`）只能有一個 App 在管理。開了第二個時，它不會檢查訂閱或接續任務，「系統」和「訂閱」頁會提示。
- 訪客直接開啟擁有者專用的網址（例如 `/library`）時，會回到下載頁，但 Streamlit 會先跳出英文的「Page not found」提示；這是 Streamlit 內建的訊息，無法改成中文。同樣無法改成中文的還有上傳框的「Upload」、「2MB per file」等字樣，以及排程日期的月曆。
- 目前 YouTube 拒絕 `tv` 和 `tv_downgraded` 用戶端（「必須重新載入頁面」）。自己選了這兩個時會看到說明，請改回「預設」或 `web_embedded`；自動重試會略過失敗的用戶端。

## 開發

```
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m pytest -m network
.venv\Scripts\ruff.exe check .
```

`pytest` 不連網，約 1,200 項測試、5–10 分鐘，用真正的 yt-dlp 和 ffmpeg 處理本機的假 YouTube 資料。`pytest -m network` 會真的連到 YouTube：約下載 15 次一部 19 秒的影片和一次 4 分鐘的音樂影片，也會搜尋、列出頻道並查詢 SponsorBlock，太常執行可能被 YouTube 暫時限制。

程式放在 `videocap/`：下載引擎與佇列不依賴 Streamlit，介面都在 `videocap/ui/`，`main.py` 只是 Streamlit 的進入點，`python -m videocap` 是啟動器。GitHub Actions 會在 Linux 和 Windows 上跑測試並建置 Docker 映像檔，也會在 macOS 上做基本檢查。
