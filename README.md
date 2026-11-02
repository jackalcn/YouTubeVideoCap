# YouTube影片下載平台

貼上 YouTube 影片、Shorts、播放清單或頻道連結，選擇畫質後下載影片、音訊與字幕。以 [Streamlit](https://streamlit.io) 和 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 建置。

請僅下載你擁有權利或已獲授權的內容（例如自己上傳的影片、Creative Commons 授權內容），並遵守 YouTube 服務條款與當地著作權法。

## 在本機執行

需要 Python 3.11 以上和 [ffmpeg](https://ffmpeg.org)。沒有 ffmpeg 仍可下載，但畫質通常只有 360p，也無法轉成 MP3 或嵌入字幕。

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run main.py
```

macOS 和 Linux 請把路徑換成 `.venv/bin/python`。

YouTube 常改版。如果開始下載失敗，先更新 yt-dlp：

```
.venv\Scripts\python.exe -m pip install -U "yt-dlp[default,deno]"
```

## 部署

線上版本：https://youtube-videocap.streamlit.app/

推送到 `main` 分支後，Streamlit Community Cloud 會自動部署；系統套件列在 `packages.txt`。

YouTube 常限制雲端主機的 IP。實測在 Streamlit Community Cloud 上，解析連結正常，但多數影片下載會被回 HTTP 403，所以要穩定下載請在本機執行。

也可以試著在 App 的「進階」區塊上傳 `cookies.txt`，但這個做法尚未實測。cookies 等同登入憑證，請只上傳到你信任的部署環境。
