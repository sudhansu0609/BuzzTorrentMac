package com.buzzcaf.buzztorrent.tv;

import android.app.Activity;
import android.app.SearchManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.speech.RecognizerIntent;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/**
 * BuzzTorrent for Google TV / Android TV.
 *
 * A full-screen WebView showing the TV page served by BuzzTorrent on the user's PC,
 * plus a small bridge ("BuzzTV") for things a web page can't do on a TV: opening
 * installed OTT apps, Google TV's search, and handing a video to another player.
 * Only the chosen PC and the bundled setup page are ever loaded in the WebView, so
 * no other site can reach the bridge.
 */
public class MainActivity extends Activity {
    static final int PORT = 8765;
    static final int REQ_VOICE = 7;
    static final String SETUP = "file:///android_asset/setup.html";
    static final Pattern SERVER = Pattern.compile("^http://(\\d{1,3}\\.){3}\\d{1,3}:\\d{1,5}$");
    static final Pattern ARCHIVE_URL = Pattern.compile("^https://([a-z0-9-]+\\.)*archive\\.org/.*");
    static final Pattern MAGNET = Pattern.compile("^magnet:\\?xt=urn:btih:[0-9a-fA-F]{40}(&[A-Za-z0-9._~%=&+:/-]*)?$");

    WebView web;
    SharedPreferences prefs;
    volatile String currentUrl = "";
    TvDownloads downloadsHelper;

    synchronized TvDownloads downloads() {
        if (downloadsHelper == null) downloadsHelper = new TvDownloads(this);
        return downloadsHelper;
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("buzztv", MODE_PRIVATE);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0b0d12"));
        web.setFocusable(true);
        web.setFocusableInTouchMode(true);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        s.setUserAgentString(s.getUserAgentString() + " BuzzTorrentTV/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new Client());
        web.addJavascriptInterface(new Bridge(), "BuzzTV");
        setContentView(web);
        web.setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        web.requestFocus();
        String server = prefs.getString("server", null);
        web.loadUrl(server == null ? SETUP : server + "/tv");
    }

    boolean allowed(String url) {
        if (url == null) return false;
        if (url.startsWith(SETUP)) return true;
        String server = prefs.getString("server", null);
        return server != null && (url.equals(server) || url.startsWith(server + "/"));
    }

    class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            String url = req.getUrl().toString();
            if (allowed(url)) return false;
            // Anything else (a web link) goes to whatever app handles it, never into this WebView.
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, req.getUrl()).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (ActivityNotFoundException e) {
                toast("No app on this TV can open that link");
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
            currentUrl = url == null ? "" : url;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame() && !req.getUrl().toString().startsWith(SETUP)) {
                String server = prefs.getString("server", "");
                view.loadUrl(SETUP + "?offline=" + Uri.encode(server));
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_VOICE || resultCode != RESULT_OK || data == null) return;
        ArrayList<String> heard = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
        if (heard == null || heard.isEmpty()) return;
        web.evaluateJavascript("window.onVoice && onVoice(" + JSONObject.quote(heard.get(0)) + ")", null);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_SEARCH || keyCode == KeyEvent.KEYCODE_VOICE_ASSIST) {
            // Remotes with a Search key: start BuzzTorrent's own voice search
            web.evaluateJavascript("window.startVoice && startVoice()", null);
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            web.evaluateJavascript("window.buzzBack ? String(window.buzzBack()) : 'false'", value -> {
                if (!"\"true\"".equals(value)) finish();
            });
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    void toast(final String msg) {
        runOnUiThread(() -> Toast.makeText(this, msg, Toast.LENGTH_LONG).show());
    }

    boolean launch(Intent intent) {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (getPackageManager().resolveActivity(intent, 0) == null) return false;
        runOnUiThread(() -> {
            try {
                startActivity(intent);
            } catch (ActivityNotFoundException | SecurityException e) {
                toast("Couldn't open that app");
            }
        });
        return true;
    }

    static String norm(String s) {
        return s == null ? "" : s.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9+]", "");
    }

    /** Launchable apps on this TV: package name and visible label. */
    List<String[]> apps() {
        PackageManager pm = getPackageManager();
        Set<String> seen = new LinkedHashSet<>();
        List<String[]> out = new ArrayList<>();
        for (String cat : new String[]{Intent.CATEGORY_LEANBACK_LAUNCHER, Intent.CATEGORY_LAUNCHER}) {
            Intent i = new Intent(Intent.ACTION_MAIN).addCategory(cat);
            for (ResolveInfo ri : pm.queryIntentActivities(i, 0)) {
                String pkg = ri.activityInfo.packageName;
                if (seen.add(pkg)) out.add(new String[]{pkg, String.valueOf(ri.loadLabel(pm))});
            }
        }
        return out;
    }

    /** The JavaScript bridge. Methods run on a background thread. */
    class Bridge {
        boolean fromOurPages() {
            return allowed(currentUrl);
        }

        @JavascriptInterface
        public String installedApps() {
            if (!fromOurPages()) return "[]";
            JSONArray a = new JSONArray();
            for (String[] app : apps()) a.put(app[1]);
            return a.toString();
        }

        @JavascriptInterface
        public String openService(String appsJson, String name, String query) {
            if (!fromOurPages()) return "none";
            List<String> wanted = new ArrayList<>();
            try {
                JSONArray a = new JSONArray(appsJson);
                for (int i = 0; i < a.length(); i++) wanted.add(norm(a.getString(i)));
            } catch (Exception e) {
                return "none";
            }
            String pkg = null;
            for (String[] app : apps()) {
                String label = norm(app[1]);
                for (String w : wanted) {
                    if (label.equals(w) || (w.length() >= 5 && label.startsWith(w))) { pkg = app[0]; break; }
                }
                if (pkg != null) break;
            }
            if (pkg == null) {
                Intent store = new Intent(Intent.ACTION_VIEW, Uri.parse("market://search?q=" + Uri.encode(name) + "&c=apps"));
                return launch(store) ? "store" : "none";
            }
            // Ask the app itself to search; many TV apps support one of these.
            Intent search = new Intent(Intent.ACTION_SEARCH).setPackage(pkg).putExtra(SearchManager.QUERY, query);
            if (launch(search)) return "search";
            Intent gsearch = new Intent("com.google.android.gms.actions.SEARCH_ACTION").setPackage(pkg)
                    .putExtra(SearchManager.QUERY, query);
            if (launch(gsearch)) return "search";
            PackageManager pm = getPackageManager();
            Intent open = pm.getLeanbackLaunchIntentForPackage(pkg);
            if (open == null) open = pm.getLaunchIntentForPackage(pkg);
            return open != null && launch(open) ? "opened" : "none";
        }

        @JavascriptInterface
        public String globalSearch(String query) {
            if (!fromOurPages()) return "none";
            Intent i = new Intent("android.search.action.GLOBAL_SEARCH").putExtra(SearchManager.QUERY, query);
            if (launch(i)) return "ok";
            Intent assist = new Intent(Intent.ACTION_WEB_SEARCH).putExtra(SearchManager.QUERY, query);
            return launch(assist) ? "ok" : "none";
        }

        /** Plays in BuzzTorrent's own full-screen player (all formats the TV can decode). */
        @JavascriptInterface
        public String playNative(String payload) {
            if (!fromOurPages() || payload == null || payload.length() > 2_000_000) return "none";
            Intent i = new Intent(MainActivity.this, PlayerActivity.class).putExtra(PlayerActivity.EXTRA, payload);
            runOnUiThread(() -> startActivity(i));
            return "ok";
        }

        /** Voice search with the TV's own speech recognizer; the text comes back via onVoice(). */
        @JavascriptInterface
        public String voiceSearch(String lang) {
            if (!fromOurPages()) return "none";
            String language = lang != null && lang.matches("[a-z]{2}-[A-Z]{2}") ? lang : "en-IN";
            Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, language)
                    .putExtra(RecognizerIntent.EXTRA_PROMPT, "hi-IN".equals(language)
                            ? "फ़िल्म, शो या गाने का नाम बोलें" : "Say a movie, show or song")
                    .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3);
            if (getPackageManager().resolveActivity(i, 0) == null) return "none";
            runOnUiThread(() -> {
                try {
                    startActivityForResult(i, REQ_VOICE);
                } catch (ActivityNotFoundException | SecurityException e) {
                    toast("Voice search isn't available on this TV");
                }
            });
            return "ok";
        }

        // ---- downloads to the TV ----------------------------------------------------
        @JavascriptInterface
        public String storage() {
            return fromOurPages() ? downloads().storage().toString() : "[]";
        }

        @JavascriptInterface
        public String downloadToTv(String json) {
            if (!fromOurPages()) return "not allowed";
            try {
                return downloads().start(new JSONObject(json));
            } catch (Exception e) {
                return "Couldn't start the download";
            }
        }

        @JavascriptInterface
        public String listDownloads() {
            return fromOurPages() ? downloads().list().toString() : "[]";
        }

        @JavascriptInterface
        public void deleteDownload(String id) {
            if (!fromOurPages()) return;
            try {
                downloads().delete(Long.parseLong(id));
            } catch (NumberFormatException ignored) {
            }
        }

        @JavascriptInterface
        public String playDownload(String id) {
            if (!fromOurPages()) return "none";
            String payload;
            try {
                payload = downloads().playPayload(Long.parseLong(id));
            } catch (NumberFormatException e) {
                return "none";
            }
            if (payload == null) return "none";
            Intent i = new Intent(MainActivity.this, PlayerActivity.class).putExtra(PlayerActivity.EXTRA, payload);
            runOnUiThread(() -> startActivity(i));
            return "ok";
        }

        /** Hands a torrent to a torrent app installed on the TV (e.g. LibreTorrent). */
        @JavascriptInterface
        public String openMagnet(String magnet) {
            if (!fromOurPages() || magnet == null || !MAGNET.matcher(magnet).matches()) return "none";
            return launch(new Intent(Intent.ACTION_VIEW, Uri.parse(magnet))) ? "ok" : "none";
        }

        @JavascriptInterface
        public boolean hasTorrentApp() {
            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse("magnet:?xt=urn:btih:0000000000000000000000000000000000000000"));
            return fromOurPages() && getPackageManager().resolveActivity(i, 0) != null;
        }

        @JavascriptInterface
        public String appVersion() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (PackageManager.NameNotFoundException e) {
                return "";
            }
        }

        @JavascriptInterface
        public String playExternal(String url, String title) {
            if (!fromOurPages() || url == null || !ARCHIVE_URL.matcher(url).matches()) return "none";
            // No chooser: the system's own picker offers "Always", so it's one press next time.
            Intent i = new Intent(Intent.ACTION_VIEW).setDataAndType(Uri.parse(url), "video/*")
                    .putExtra(Intent.EXTRA_TITLE, title).putExtra("title", title);
            if (launch(i)) return "ok";
            toast("Install a video player such as VLC first");
            return "none";
        }

        @JavascriptInterface
        public void resetServer() {
            if (!fromOurPages()) return;
            prefs.edit().remove("server").apply();
            runOnUiThread(() -> web.loadUrl(SETUP));
        }

        @JavascriptInterface
        public String savedServer() {
            return fromOurPages() ? prefs.getString("server", "") : "";
        }

        @JavascriptInterface
        public boolean setServer(String url) {
            // Only the bundled setup page may change which PC this app trusts.
            if (!currentUrl.startsWith(SETUP) || url == null || !SERVER.matcher(url).matches()) return false;
            prefs.edit().putString("server", url).apply();
            runOnUiThread(() -> web.loadUrl(url + "/tv"));
            return true;
        }

        @JavascriptInterface
        public void startDiscover() {
            if (!currentUrl.startsWith(SETUP)) return;
            new Thread(() -> {
                List<String> found = discover();
                JSONArray a = new JSONArray(found);
                runOnUiThread(() -> web.evaluateJavascript("window.onDiscover && onDiscover(" + a + ")", null));
            }, "discover").start();
        }

        @JavascriptInterface
        public String check(String url) {
            if (!currentUrl.startsWith(SETUP) || url == null || !SERVER.matcher(url).matches()) return "bad";
            return hello(url) ? "ok" : "none";
        }
    }

    /** Finds BuzzTorrent servers on this TV's /24 network(s). */
    List<String> discover() {
        Set<String> prefixes = new LinkedHashSet<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        String ip = a.getHostAddress();
                        prefixes.add(ip.substring(0, ip.lastIndexOf('.') + 1));
                    }
                }
            }
        } catch (Exception ignored) {
        }
        final List<String> found = Collections.synchronizedList(new ArrayList<>());
        ExecutorService pool = Executors.newFixedThreadPool(48);
        for (String prefix : prefixes) {
            for (int i = 1; i < 255; i++) {
                final String host = prefix + i;
                pool.execute(() -> {
                    try (Socket sock = new Socket()) {
                        sock.connect(new InetSocketAddress(host, PORT), 350);
                    } catch (Exception e) {
                        return;
                    }
                    String url = "http://" + host + ":" + PORT;
                    if (hello(url)) found.add(url);
                });
            }
        }
        pool.shutdown();
        try {
            pool.awaitTermination(15, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
        }
        return new ArrayList<>(found);
    }

    static boolean hello(String base) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/tv/hello").openConnection();
            c.setConnectTimeout(1500);
            c.setReadTimeout(2500);
            if (c.getResponseCode() != 200) return false;
            InputStream in = c.getInputStream();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[2048];
            int n;
            while ((n = in.read(b)) > 0 && buf.size() < 8192) buf.write(b, 0, n);
            return "BuzzTorrent".equals(new JSONObject(buf.toString("UTF-8")).optString("app"));
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
