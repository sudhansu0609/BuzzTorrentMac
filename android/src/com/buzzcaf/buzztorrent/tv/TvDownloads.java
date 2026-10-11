package com.buzzcaf.buzztorrent.tv;

import android.app.DownloadManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;
import android.os.Environment;
import android.os.StatFs;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.IOException;

/**
 * Saves free Internet Archive videos onto the TV (or a USB drive plugged into it) for
 * offline viewing. Uses Android's DownloadManager, which resumes after Wi-Fi drops and
 * keeps going in the background. Files go in this app's own folder on each storage, so no
 * storage permission is needed; they are removed if the app is uninstalled (an update
 * keeps them).
 */
class TvDownloads {
    final Context ctx;
    final SharedPreferences prefs;
    final DownloadManager dm;

    TvDownloads(Context ctx) {
        this.ctx = ctx;
        prefs = ctx.getSharedPreferences("downloads", Context.MODE_PRIVATE);
        dm = (DownloadManager) ctx.getSystemService(Context.DOWNLOAD_SERVICE);
    }

    File[] dirs() {
        return ctx.getExternalFilesDirs(Environment.DIRECTORY_MOVIES);
    }

    /** Places a video can be saved: the TV's own storage first, then USB drives / SD cards. */
    JSONArray storage() {
        JSONArray a = new JSONArray();
        File[] dirs = dirs();
        for (int i = 0; i < dirs.length; i++) {
            File d = dirs[i];
            if (d == null || !Environment.MEDIA_MOUNTED.equals(Environment.getExternalStorageState(d))) continue;
            try {
                d.mkdirs();
                StatFs st = new StatFs(d.getPath());
                boolean removable = Environment.isExternalStorageRemovable(d);
                a.put(new JSONObject().put("id", i).put("free", st.getAvailableBytes()).put("total", st.getTotalBytes())
                        .put("removable", removable).put("label", removable ? "USB drive / SD card" : "TV storage"));
            } catch (Exception ignored) {
            }
        }
        return a;
    }

    JSONArray records() {
        try {
            return new JSONArray(prefs.getString("list", "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    void save(JSONArray list) {
        prefs.edit().putString("list", list.toString()).apply();
    }

    static String safeName(String s) {
        String t = s == null ? "" : s.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", " ").replaceAll("\\s+", " ").trim();
        if (t.length() > 80) t = t.substring(0, 80).trim();
        return t.isEmpty() ? "video" : t;
    }

    /** Returns "ok", "fallback" (saved to TV storage instead of the chosen drive) or an error. */
    String start(JSONObject o) {
        String url = o.optString("url");
        if (!MainActivity.ARCHIVE_URL.matcher(url).matches()) return "Only free Internet Archive videos can be saved";
        String title = o.optString("title", "Video"), label = o.optString("label", "");
        int storage = o.optInt("storage", 0);
        String path = Uri.parse(url).getLastPathSegment();
        String ext = path != null && path.lastIndexOf('.') > 0 ? path.substring(path.lastIndexOf('.')) : ".mp4";
        String base = safeName(title) + (label.isEmpty() ? "" : " [" + safeName(label) + "]");
        File[] dirs = dirs();
        String result = "ok";
        for (int attempt = 0; attempt < 2; attempt++) {
            int idx = attempt == 0 ? storage : 0;
            if (idx < 0 || idx >= dirs.length || dirs[idx] == null) { idx = 0; result = "fallback"; }
            File dir = dirs[idx];
            if (dir == null) return "No storage available on this TV";
            dir.mkdirs();
            File f = new File(dir, base + ext);
            for (int n = 2; f.exists(); n++) f = new File(dir, base + " (" + n + ")" + ext);
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url))
                        .setTitle(title + (label.isEmpty() ? "" : " · " + label))
                        .setDescription("BuzzTorrent")
                        .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                        .setAllowedOverMetered(true)
                        .setDestinationUri(Uri.fromFile(f));
                long id = dm.enqueue(req);
                JSONArray list = records();
                list.put(new JSONObject().put("id", id).put("title", title).put("label", label)
                        .put("size", o.optLong("size", 0)).put("path", f.getAbsolutePath())
                        .put("added", System.currentTimeMillis()));
                save(list);
                return attempt == 0 ? result : "fallback";
            } catch (Exception e) {
                if (idx == 0) return "Couldn't start the download: " + e.getMessage();
            }
        }
        return "Couldn't start the download";
    }

    /** Every saved or saving video, newest first, with live progress. */
    JSONArray list() {
        JSONArray recs = records(), out = new JSONArray();
        for (int i = recs.length() - 1; i >= 0; i--) {
            JSONObject r = recs.optJSONObject(i);
            if (r == null) continue;
            long id = r.optLong("id");
            String status = "missing";
            long done = 0, total = r.optLong("size", 0);
            Cursor c = null;
            try {
                c = dm.query(new DownloadManager.Query().setFilterById(id));
                if (c != null && c.moveToFirst()) {
                    int st = c.getInt(c.getColumnIndex(DownloadManager.COLUMN_STATUS));
                    done = c.getLong(c.getColumnIndex(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                    long t = c.getLong(c.getColumnIndex(DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
                    if (t > 0) total = t;
                    status = st == DownloadManager.STATUS_SUCCESSFUL ? "done" : st == DownloadManager.STATUS_FAILED ? "failed"
                            : st == DownloadManager.STATUS_PAUSED ? "paused" : st == DownloadManager.STATUS_RUNNING ? "running" : "pending";
                }
            } catch (Exception ignored) {
            } finally {
                if (c != null) c.close();
            }
            File f = new File(r.optString("path"));
            if ("missing".equals(status) && f.isFile()) { status = "done"; done = total = f.length(); }
            if ("done".equals(status) && !f.isFile()) status = "missing";
            try {
                out.put(new JSONObject().put("id", id).put("title", r.optString("title")).put("label", r.optString("label"))
                        .put("status", status).put("done", done).put("total", total)
                        .put("removable", isRemovable(f)));
            } catch (Exception ignored) {
            }
        }
        return out;
    }

    static boolean isRemovable(File f) {
        try {
            return Environment.isExternalStorageRemovable(f);
        } catch (Exception e) {
            return false;
        }
    }

    JSONObject find(long id) {
        JSONArray recs = records();
        for (int i = 0; i < recs.length(); i++) {
            JSONObject r = recs.optJSONObject(i);
            if (r != null && r.optLong("id") == id) return r;
        }
        return null;
    }

    void delete(long id) {
        JSONObject r = find(id);
        try {
            dm.remove(id);
        } catch (Exception ignored) {
        }
        if (r != null) {
            File f = new File(r.optString("path"));
            if (inOurDirs(f)) f.delete();
        }
        JSONArray recs = records(), keep = new JSONArray();
        for (int i = 0; i < recs.length(); i++) {
            JSONObject x = recs.optJSONObject(i);
            if (x != null && x.optLong("id") != id) keep.put(x);
        }
        save(keep);
    }

    /** Player payload for a finished download, or null. */
    String playPayload(long id) {
        JSONObject r = find(id);
        if (r == null) return null;
        File f = new File(r.optString("path"));
        if (!f.isFile() || !inOurDirs(f)) return null;
        try {
            JSONObject item = new JSONObject().put("name", r.optString("title"))
                    .put("urls", new JSONArray().put(Uri.fromFile(f).toString()))
                    .put("labels", new JSONArray().put(r.optString("label")));
            return new JSONObject().put("title", r.optString("title")).put("index", 0)
                    .put("items", new JSONArray().put(item)).toString();
        } catch (Exception e) {
            return null;
        }
    }

    /** True for files inside this app's own folders (where downloads are saved). */
    boolean inOurDirs(File f) {
        try {
            String p = f.getCanonicalPath();
            for (File d : ctx.getExternalFilesDirs(null)) {
                if (d != null && p.startsWith(d.getCanonicalPath() + File.separator)) return true;
            }
        } catch (IOException ignored) {
        }
        return false;
    }
}
