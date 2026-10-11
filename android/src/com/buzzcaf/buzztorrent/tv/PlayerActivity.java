package com.buzzcaf.buzztorrent.tv;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.VideoView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Full-screen native player using the TV's own decoders (MKV, HEVC, 1080p, 4K).
 *
 * - Each video comes with all its copies, sharpest first. Before playing, a short Wi-Fi speed
 *   check picks the best copy the connection can keep up with; if it still keeps buffering,
 *   it steps down a quality by itself. ▼ changes quality by hand.
 * - ▲ cycles subtitles (drawn here from a cue list the PC prepares from .srt/.vtt files).
 * - The position is saved on the PC every few seconds, so "Continue watching" works from
 *   any TV or phone, and playback resumes where it stopped.
 */
public class PlayerActivity extends Activity {
    static final String EXTRA = "payload";

    static class Sub {
        String label, url;
        double[] start, end;
        String[] text;
        boolean loading, failed;
    }

    static class Item {
        String name, key, thumb;
        double resume;
        List<String> urls = new ArrayList<>(), labels = new ArrayList<>();
        List<Long> bitrates = new ArrayList<>();
        List<Sub> subs = new ArrayList<>();
    }

    final List<Item> items = new ArrayList<>();
    String title = "", progressUrl = "";
    int item, version, sub = -1;
    int resumeAt;              // ms to seek to once prepared
    boolean prepared, manualQuality, resumed;
    long preparedAt;
    final List<Long> stalls = new ArrayList<>();
    int ticks;

    VideoView video;
    LinearLayout hud;
    TextView hudTitle, hudTime, hudHint, message, subtitle;
    ProgressBar bar;
    final Handler ui = new Handler(Looper.getMainLooper());
    final Runnable tick = new Runnable() {
        @Override
        public void run() {
            updateHud();
            drawSubtitle();
            if (++ticks % 75 == 0 && prepared && video.isPlaying()) saveProgress(false);   // every 15 s
            ui.postDelayed(this, 200);
        }
    };
    final Runnable hideHud = () -> {
        if (video.isPlaying()) hud.setVisibility(View.GONE);
    };
    final Runnable hideMessage = () -> {
        if (prepared) message.setVisibility(View.GONE);
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON | WindowManager.LayoutParams.FLAG_FULLSCREEN);
        if (!parse(getIntent().getStringExtra(EXTRA))) {
            finish();
            return;
        }
        buildUi();
        start(item);
        ui.post(tick);
    }

    // ------------------------------------------------------------------ input
    String serverBase() {
        return getSharedPreferences("buzztv", MODE_PRIVATE).getString("server", "");
    }

    boolean allowedUrl(String u) {
        if (u == null) return false;
        if (MainActivity.ARCHIVE_URL.matcher(u).matches()) return true;
        String server = serverBase();
        if (!server.isEmpty() && u.startsWith(server + "/api/tv/")) return true;      // this PC's library / subtitles
        if (!u.startsWith("file://")) return false;
        String path = Uri.parse(u).getPath();
        return path != null && new TvDownloads(this).inOurDirs(new java.io.File(path));
    }

    boolean parse(String json) {
        try {
            JSONObject o = new JSONObject(json);
            title = o.optString("title", "");
            item = o.optInt("index", 0);
            String prog = o.optString("progress", "");
            if (allowedUrl(prog)) progressUrl = prog;
            JSONArray arr = o.getJSONArray("items");
            for (int i = 0; i < arr.length(); i++) {
                JSONObject it = arr.getJSONObject(i);
                Item x = new Item();
                x.name = it.optString("name", "");
                x.key = it.optString("key", "");
                x.thumb = it.optString("thumb", "");
                x.resume = it.optDouble("resume", 0);
                JSONArray urls = it.getJSONArray("urls");
                JSONArray labels = it.optJSONArray("labels");
                JSONArray rates = it.optJSONArray("bitrates");
                for (int j = 0; j < urls.length(); j++) {
                    String u = urls.getString(j);
                    if (!allowedUrl(u)) continue;
                    x.urls.add(u);
                    x.labels.add(labels != null && j < labels.length() ? labels.optString(j, "") : "");
                    x.bitrates.add(rates != null && j < rates.length() ? rates.optLong(j, 0) : 0L);
                }
                JSONArray subs = it.optJSONArray("subs");
                for (int j = 0; subs != null && j < subs.length(); j++) {
                    JSONObject s = subs.getJSONObject(j);
                    Sub sb = new Sub();
                    sb.label = s.optString("label", "Subtitles");
                    sb.url = s.optString("url", "");
                    if (allowedUrl(sb.url)) x.subs.add(sb);
                }
                if (!x.urls.isEmpty()) items.add(x);
            }
        } catch (Exception e) {
            return false;
        }
        if (items.isEmpty()) return false;
        item = Math.max(0, Math.min(item, items.size() - 1));
        return true;
    }

    // ---------------------------------------------------------------- layout
    int dp(float v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    TextView text(float sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setTextSize(sp);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    void buildUi() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        video = new VideoView(this);
        root.addView(video, new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));

        subtitle = text(26, Color.WHITE, true);
        subtitle.setGravity(Gravity.CENTER);
        subtitle.setShadowLayer(6, 0, 2, Color.BLACK);
        subtitle.setPadding(dp(60), 0, dp(60), 0);
        FrameLayout.LayoutParams sl = new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        sl.bottomMargin = dp(48);
        root.addView(subtitle, sl);

        message = text(20, Color.WHITE, true);
        message.setGravity(Gravity.CENTER);
        message.setPadding(dp(48), dp(24), dp(48), dp(24));
        GradientDrawable pill = new GradientDrawable();
        pill.setColor(0xCC151A24);
        pill.setCornerRadius(dp(16));
        message.setBackground(pill);
        root.addView(message, new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER));

        hud = new LinearLayout(this);
        hud.setOrientation(LinearLayout.VERTICAL);
        hud.setPadding(dp(48), dp(40), dp(48), dp(28));
        hud.setBackground(new GradientDrawable(GradientDrawable.Orientation.BOTTOM_TOP, new int[]{0xE6000000, 0x00000000}));
        hudTitle = text(20, Color.WHITE, true);
        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(1000);
        bar.getProgressDrawable().setTint(0xFFFFC531);
        LinearLayout row = new LinearLayout(this);
        hudTime = text(14, 0xFFB0B7C8, false);
        hudHint = text(14, 0xFF7D869B, false);
        hudHint.setGravity(Gravity.END);
        row.addView(hudTime, new LinearLayout.LayoutParams(0, -2, 1));
        row.addView(hudHint, new LinearLayout.LayoutParams(0, -2, 3));
        hud.addView(hudTitle);
        hud.addView(bar, new LinearLayout.LayoutParams(-1, dp(10)));
        hud.addView(row);
        root.addView(hud, new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM));
        setContentView(root);

        video.setOnPreparedListener(mp -> {
            prepared = true;
            preparedAt = System.currentTimeMillis();
            message.setVisibility(View.GONE);
            mp.setOnInfoListener((p, what, extra) -> {
                if (what == MediaPlayer.MEDIA_INFO_BUFFERING_START) {
                    show("Buffering…");
                    stalled();
                } else if (what == MediaPlayer.MEDIA_INFO_BUFFERING_END) {
                    message.setVisibility(View.GONE);
                }
                return false;
            });
            Item it = items.get(item);
            if (resumeAt == 0 && !resumed && it.resume > 0) {
                resumeAt = (int) (it.resume * 1000);
                flash("Resumed from " + time(resumeAt) + ". Hold ◀ to go back");
            }
            resumed = true;
            if (resumeAt > 0) video.seekTo(resumeAt);
            resumeAt = 0;
            video.start();
            showHud();
        });
        video.setOnErrorListener((mp, what, extra) -> {
            Item it = items.get(item);
            int pos = prepared ? video.getCurrentPosition() : resumeAt;
            if (version + 1 < it.urls.size()) {
                flash(label(it, version) + " can't play on this TV. Trying " + label(it, version + 1) + "…");
                play(item, version + 1, pos);
            } else {
                show("This video can't play on this TV.\nPress Back to choose another copy.");
            }
            return true;
        });
        video.setOnCompletionListener(mp -> {
            saveProgress(true);
            if (item + 1 < items.size()) start(item + 1);
            else finish();
        });
    }

    // -------------------------------------------------------------- playback
    String label(Item it, int v) {
        String l = it.labels.get(v);
        return l == null || l.isEmpty() ? "This copy" : l;
    }

    /** Starts an item: picks the quality the Wi-Fi can carry, then plays. */
    void start(int i) {
        item = i;
        sub = -1;
        resumed = false;
        Item it = items.get(i);
        boolean remote = it.urls.get(0).startsWith("https://");
        if (manualQuality || it.urls.size() < 2 || !remote) {
            play(i, manualQuality ? Math.min(version, it.urls.size() - 1) : 0, 0);
            return;
        }
        show("Checking your Wi-Fi speed…");
        new Thread(() -> {
            double mbps = measureMbps(it.urls.get(0));
            int pick = it.urls.size() - 1;
            for (int v = 0; v < it.urls.size(); v++) {
                if (needMbps(it, v) <= mbps / 1.25) { pick = v; break; }
            }
            final int chosen = mbps <= 0 ? 0 : pick;
            final String note = mbps <= 0 ? "" : String.format(Locale.ROOT, "Wi-Fi ≈ %.0f Mbps · playing %s", mbps, label(it, chosen))
                    + (chosen > 0 ? " (" + label(it, 0) + " needs more). ▼ to change" : "");
            runOnUiThread(() -> {
                if (isFinishing()) return;
                play(i, chosen, 0);
                if (!note.isEmpty()) flash(note);
            });
        }, "speedtest").start();
    }

    /** Rough bandwidth a copy needs: its real bitrate when known, else by resolution. */
    double needMbps(Item it, int v) {
        long bps = it.bitrates.get(v);
        if (bps > 0) return bps / 1e6 * 1.2;
        String l = it.labels.get(v);
        switch (l == null ? "" : l) {
            case "8K": return 60;
            case "4K": return 20;
            case "1080p": return 6;
            case "720p": return 3;
            case "480p": return 1.5;
            case "Low": return 0.8;
            default: return 4;
        }
    }

    /** Downloads up to 4 MB of the video for at most 5 s and returns the speed in Mbps (0 = unknown). */
    static double measureMbps(String url) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(5000);
            c.setReadTimeout(5000);
            c.setRequestProperty("Range", "bytes=0-4194303");
            InputStream in = c.getInputStream();
            byte[] buf = new byte[64 * 1024];
            long first = 0, total = 0;
            long deadline = System.currentTimeMillis() + 5000;
            int n;
            while ((n = in.read(buf)) > 0) {
                if (first == 0) first = System.nanoTime();
                total += n;
                if (total >= 4194304 || System.currentTimeMillis() > deadline) break;
            }
            double secs = (System.nanoTime() - first) / 1e9;
            return total < 256 * 1024 || secs <= 0 ? 0 : total * 8 / secs / 1e6;
        } catch (Exception e) {
            return 0;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    void play(int i, int v, int positionMs) {
        item = i;
        version = v;
        prepared = false;
        resumeAt = positionMs;
        stalls.clear();
        Item it = items.get(i);
        show("Loading " + label(it, v) + "…");
        video.setVideoURI(Uri.parse(it.urls.get(v)));
        showHud();
    }

    /** Keeps buffering? Step down one quality (unless the viewer picked one by hand). */
    void stalled() {
        long now = System.currentTimeMillis();
        if (now - preparedAt < 15000) return;                 // the first seconds always buffer
        stalls.add(now);
        while (!stalls.isEmpty() && now - stalls.get(0) > 120000) stalls.remove(0);
        Item it = items.get(item);
        if (stalls.size() >= 3 && !manualQuality && version + 1 < it.urls.size()) {
            flash("Your Wi-Fi can't keep up. Switching to " + label(it, version + 1));
            play(item, version + 1, video.getCurrentPosition());
        }
    }

    // ------------------------------------------------------------- subtitles
    void cycleSubtitle() {
        Item it = items.get(item);
        if (it.subs.isEmpty()) {
            flash("No subtitles for this video");
            return;
        }
        sub = sub + 1 >= it.subs.size() ? -1 : sub + 1;
        subtitle.setText("");
        if (sub < 0) {
            flash("Subtitles off");
            return;
        }
        Sub s = it.subs.get(sub);
        flash("Subtitles: " + s.label);
        if (s.text == null && !s.loading) loadSub(s);
    }

    void loadSub(Sub s) {
        s.loading = true;
        new Thread(() -> {
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(s.url).openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(15000);
                InputStream in = c.getInputStream();
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] b = new byte[16384];
                int n;
                while ((n = in.read(b)) > 0 && buf.size() < 8 * 1024 * 1024) buf.write(b, 0, n);
                JSONArray cues = new JSONObject(buf.toString("UTF-8")).getJSONArray("cues");
                double[] st = new double[cues.length()], en = new double[cues.length()];
                String[] tx = new String[cues.length()];
                for (int i = 0; i < cues.length(); i++) {
                    JSONObject q = cues.getJSONObject(i);
                    st[i] = q.optDouble("start");
                    en[i] = q.optDouble("end");
                    tx[i] = q.optString("text");
                }
                s.start = st;
                s.end = en;
                s.text = tx;
            } catch (Exception e) {
                s.failed = true;
                runOnUiThread(() -> flash("Couldn't load the subtitles"));
            } finally {
                s.loading = false;
                if (c != null) c.disconnect();
            }
        }, "subtitles").start();
    }

    void drawSubtitle() {
        if (sub < 0 || !prepared) {
            if (subtitle.length() > 0) subtitle.setText("");
            return;
        }
        Sub s = items.get(item).subs.get(sub);
        if (s.text == null) return;
        double t = video.getCurrentPosition() / 1000.0;
        int lo = 0, hi = s.start.length - 1, found = -1;
        while (lo <= hi) {                                  // last cue starting at or before t
            int mid = (lo + hi) >>> 1;
            if (s.start[mid] <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
        }
        String line = found >= 0 && t <= s.end[found] ? s.text[found] : "";
        if (!line.contentEquals(subtitle.getText())) subtitle.setText(line);
    }

    // -------------------------------------------------------------- progress
    void saveProgress(boolean finished) {
        if (progressUrl.isEmpty() || items.isEmpty()) return;
        Item it = items.get(item);
        if (it.key.isEmpty()) return;
        int dur = prepared ? video.getDuration() : 0;
        int pos = finished ? dur : (prepared ? video.getCurrentPosition() : 0);
        if (dur <= 0) return;
        final String body;
        try {
            body = new JSONObject().put("key", it.key).put("pos", pos / 1000.0).put("dur", dur / 1000.0)
                    .put("title", title).put("name", items.size() > 1 ? it.name : "")
                    .put("thumb", it.thumb).put("quality", label(it, version)).toString();
        } catch (Exception e) {
            return;
        }
        final String url = progressUrl;
        new Thread(() -> {
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(4000);
                c.setReadTimeout(4000);
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                c.setRequestProperty("X-BuzzTorrent", "1");
                try (OutputStream out = c.getOutputStream()) {
                    out.write(body.getBytes("UTF-8"));
                }
                c.getResponseCode();
            } catch (Exception ignored) {
            } finally {
                if (c != null) c.disconnect();
            }
        }, "progress").start();
    }

    // ------------------------------------------------------------------- HUD
    void show(String msg) {
        ui.removeCallbacks(hideMessage);
        message.setText(msg);
        message.setVisibility(View.VISIBLE);
    }

    void flash(String msg) {
        show(msg);
        ui.postDelayed(hideMessage, 3500);
    }

    static String time(int ms) {
        int s = Math.max(0, ms / 1000);
        int h = s / 3600, m = (s % 3600) / 60, sec = s % 60;
        return h > 0 ? String.format(Locale.ROOT, "%d:%02d:%02d", h, m, sec) : String.format(Locale.ROOT, "%d:%02d", m, sec);
    }

    void updateHud() {
        if (video == null || items.isEmpty()) return;
        Item it = items.get(item);
        String name = items.size() > 1 ? title + " · " + it.name : title;
        String subLabel = sub >= 0 ? " · CC " + it.subs.get(sub).label : "";
        hudTitle.setText(name + "   [" + label(it, version) + (manualQuality ? "" : " auto") + subLabel + "]");
        int dur = prepared ? video.getDuration() : 0, pos = prepared ? video.getCurrentPosition() : 0;
        bar.setProgress(dur > 0 ? (int) (1000L * pos / dur) : 0);
        hudTime.setText(time(pos) + " / " + time(dur));
        StringBuilder hint = new StringBuilder("OK pause · ◀ ▶ skip");
        if (!it.subs.isEmpty()) hint.append(" · ▲ subtitles");
        if (it.urls.size() > 1) hint.append(" · ▼ quality");
        if (items.size() > 1) hint.append(" · ⏭ next");
        hint.append(" · Back");
        hudHint.setText(hint);
    }

    void showHud() {
        updateHud();
        hud.setVisibility(View.VISIBLE);
        ui.removeCallbacks(hideHud);
        ui.postDelayed(hideHud, 4000);
    }

    void seekBy(int ms) {
        if (!prepared) return;
        int dur = video.getDuration();
        video.seekTo(Math.max(0, Math.min(dur > 0 ? dur - 1000 : Integer.MAX_VALUE, video.getCurrentPosition() + ms)));
        showHud();
    }

    /** ▼: next lower quality, wrapping back to the best. Turns automatic switching off. */
    void nextQuality() {
        Item it = items.get(item);
        if (it.urls.size() < 2) {
            flash("Only one copy of this video");
            return;
        }
        manualQuality = true;
        int v = (version + 1) % it.urls.size();
        flash("Quality: " + label(it, v));
        play(item, v, prepared ? video.getCurrentPosition() : resumeAt);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        int step = event.getRepeatCount() > 2 ? 30000 : 10000;
        switch (keyCode) {
            case KeyEvent.KEYCODE_DPAD_CENTER:
            case KeyEvent.KEYCODE_ENTER:
            case KeyEvent.KEYCODE_SPACE:
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE:
                if (event.getRepeatCount() > 0) return true;
                if (video.isPlaying()) { video.pause(); saveProgress(false); } else video.start();
                showHud();
                return true;
            case KeyEvent.KEYCODE_MEDIA_PLAY:
                video.start(); showHud(); return true;
            case KeyEvent.KEYCODE_MEDIA_PAUSE:
                video.pause(); saveProgress(false); showHud(); return true;
            case KeyEvent.KEYCODE_DPAD_LEFT:
                seekBy(-step); return true;
            case KeyEvent.KEYCODE_DPAD_RIGHT:
                seekBy(step); return true;
            case KeyEvent.KEYCODE_MEDIA_REWIND:
                seekBy(-30000); return true;
            case KeyEvent.KEYCODE_MEDIA_FAST_FORWARD:
                seekBy(30000); return true;
            case KeyEvent.KEYCODE_DPAD_UP:
            case KeyEvent.KEYCODE_CAPTIONS:
                if (event.getRepeatCount() == 0) cycleSubtitle();
                showHud();
                return true;
            case KeyEvent.KEYCODE_DPAD_DOWN:
                if (event.getRepeatCount() == 0) nextQuality();
                return true;
            case KeyEvent.KEYCODE_MEDIA_NEXT:
                if (item + 1 < items.size()) { saveProgress(false); start(item + 1); }
                return true;
            case KeyEvent.KEYCODE_MEDIA_PREVIOUS:
                if (item > 0) { saveProgress(false); start(item - 1); }
                return true;
            default:
                return super.onKeyDown(keyCode, event);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (video != null && prepared) saveProgress(false);
        if (video != null && video.isPlaying()) video.pause();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        if (video != null) video.stopPlayback();
        super.onDestroy();
    }
}
