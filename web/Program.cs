using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Caching.Memory;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddMemoryCache();
builder.Services.AddHttpClient("yahoo", c =>
{
    c.Timeout = TimeSpan.FromSeconds(10);
    c.DefaultRequestHeaders.UserAgent.ParseAdd(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36");
});

var app = builder.Build();

var dbPath = Path.GetFullPath(
    app.Configuration["DbPath"]
    ?? Path.Combine(app.Environment.ContentRootPath, "..", "data", "stocks.db"));
var connString = new SqliteConnectionStringBuilder { DataSource = dbPath }.ToString();

SqliteConnection Open()
{
    var con = new SqliteConnection(connString);
    con.Open();
    using var cmd = con.CreateCommand();
    cmd.CommandText = "PRAGMA busy_timeout=5000";
    cmd.ExecuteNonQuery();
    return con;
}

List<Dictionary<string, object?>> Query(string sql, params (string name, object? value)[] args)
{
    using var con = Open();
    using var cmd = con.CreateCommand();
    cmd.CommandText = sql;
    foreach (var (name, value) in args)
        cmd.Parameters.AddWithValue(name, value ?? DBNull.Value);
    using var reader = cmd.ExecuteReader();
    var rows = new List<Dictionary<string, object?>>();
    while (reader.Read())
    {
        var row = new Dictionary<string, object?>();
        for (var i = 0; i < reader.FieldCount; i++)
            row[reader.GetName(i)] = reader.IsDBNull(i) ? null : reader.GetValue(i);
        rows.Add(row);
    }
    return rows;
}

int Execute(string sql, params (string name, object? value)[] args)
{
    using var con = Open();
    using var cmd = con.CreateCommand();
    cmd.CommandText = sql;
    foreach (var (name, value) in args)
        cmd.Parameters.AddWithValue(name, value ?? DBNull.Value);
    return cmd.ExecuteNonQuery();
}

// JSON columns (flags_json, …) are stored as text; surface them as real JSON.
object? ParseJson(object? text) =>
    text is string s && s.Length > 0 ? JsonSerializer.Deserialize<JsonElement>(s) : null;

// EMA with SMA seed — same convention as worker/indicators.py (chart overlay only,
// all decision numbers still come exclusively from the Python worker).
double?[] EmaSeries(double[] values, int period)
{
    var output = new double?[values.Length];
    if (values.Length < period) return output;
    var k = 2.0 / (period + 1);
    var seed = values.Take(period).Average();
    output[period - 1] = seed;
    var prev = seed;
    for (var i = period; i < values.Length; i++)
    {
        prev = values[i] * k + prev * (1 - k);
        output[i] = prev;
    }
    return output;
}

// --------------------------------------------------------------------------
// Python-Bridge — Strategie-Liste + Backtests kommen aus worker/backtest.py.
// Numerik bleibt ausschließlich in Python; der Backtest liest die DB read-only.
// --------------------------------------------------------------------------

var workerDir = Path.GetFullPath(Path.Combine(app.Environment.ContentRootPath, "..", "worker"));
var pythonPath = app.Configuration["PythonPath"];
if (string.IsNullOrEmpty(pythonPath))
{
    var candidates = new[]
    {
        Path.Combine(app.Environment.ContentRootPath, "..", ".venv", "Scripts", "python.exe"),
        Path.Combine(app.Environment.ContentRootPath, "..", ".venv", "bin", "python"),
        Path.Combine(app.Environment.ContentRootPath, "..", "venv", "bin", "python"),
    };
    pythonPath = candidates.FirstOrDefault(File.Exists) is string venv
        ? Path.GetFullPath(venv)
        : (OperatingSystem.IsWindows() ? "python" : "python3");
}

async Task<(int Code, string Stdout, string Stderr)> RunPython(string[] args, int timeoutSeconds = 30)
{
    var psi = new ProcessStartInfo
    {
        FileName = pythonPath,
        WorkingDirectory = workerDir,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        UseShellExecute = false,
    };
    psi.Environment["PYTHONUTF8"] = "1";
    psi.ArgumentList.Add("backtest.py");
    foreach (var a in args) psi.ArgumentList.Add(a);
    using var proc = Process.Start(psi)!;
    var stdout = proc.StandardOutput.ReadToEndAsync();
    var stderr = proc.StandardError.ReadToEndAsync();
    using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds));
    try { await proc.WaitForExitAsync(cts.Token); }
    catch (OperationCanceledException)
    {
        try { proc.Kill(entireProcessTree: true); } catch { /* bereits beendet */ }
        return (-1, "", "timeout");
    }
    return (proc.ExitCode, await stdout, await stderr);
}

// Katalog = NAME/LABEL/PARAMS-Schema aller Plugins (Quelle: Python, gecacht).
async Task<JsonElement?> StrategyCatalog()
{
    var cache = app.Services.GetRequiredService<IMemoryCache>();
    if (cache.TryGetValue("strategies:catalog", out JsonElement cached)) return cached;
    var (code, stdout, stderr) = await RunPython(new[] { "list" });
    if (code != 0)
    {
        app.Logger.LogError("backtest.py list fehlgeschlagen ({Code}): {Err}", code, stderr);
        return null;
    }
    var catalog = JsonSerializer.Deserialize<JsonElement>(stdout);
    cache.Set("strategies:catalog", catalog, TimeSpan.FromMinutes(5));
    return catalog;
}

JsonElement? FindStrategySchema(JsonElement catalog, string name)
{
    foreach (var s in catalog.EnumerateArray())
        if (s.GetProperty("name").GetString() == name)
            return s.GetProperty("params");
    return null;
}

// Risk-Overlay (Stop-Loss / Trailing-Stop) validieren → kanonisches JSON.
// Spiegel der Regeln in worker/backtest.py parse_risk.
(string? Canonical, string? Error) ValidateRisk(string? raw)
{
    if (string.IsNullOrWhiteSpace(raw)) return (null, null);
    JsonElement el;
    try { el = JsonSerializer.Deserialize<JsonElement>(raw); }
    catch (JsonException) { return (null, "risk: kein gültiges JSON"); }
    if (el.ValueKind != JsonValueKind.Object) return (null, "risk: JSON-Objekt erwartet");
    var result = new SortedDictionary<string, double>();
    foreach (var p in el.EnumerateObject())
    {
        if (p.Name is not ("stop_loss_pct" or "trail_pct"))
            return (null, $"risk: unbekannter Key '{p.Name}'");
        if (p.Value.ValueKind == JsonValueKind.Null) continue;
        if (p.Value.ValueKind != JsonValueKind.Number)
            return (null, "risk: nur numerische Werte erlaubt");
        var v = p.Value.GetDouble();
        if (v < 0.1 || v > 50)
            return (null, "risk: Werte zwischen 0.1 und 50 (Prozent)");
        result[p.Name] = v;
    }
    return (result.Count == 0 ? null : JsonSerializer.Serialize(result), null);
}

// Slippage in Basispunkten je Fill-Seite; null = Python-Default (5 bp).
// Spiegel der Regeln in worker/backtest.py _parse_common.
(string? Canonical, string? Error) ValidateSlippage(string? raw)
{
    if (string.IsNullOrWhiteSpace(raw)) return (null, null);
    if (!double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var v)
        || v < 0 || v > 100)
        return (null, "slippage: Zahl zwischen 0 und 100 (Basispunkte)");
    return (v.ToString(CultureInfo.InvariantCulture), null);
}

// Normalerweise legt der Worker das Schema an (inkl. Migration der alten
// Ein-PK-Variante); falls das Web zuerst dran ist.
void EnsureStrategyTable() => Execute(
    "CREATE TABLE IF NOT EXISTS strategy_config(" +
    "name TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1d', " +
    "params_json TEXT, active INTEGER NOT NULL DEFAULT 0, " +
    "PRIMARY KEY (name, timeframe))");

// Params gegen das PARAMS-Schema klemmen; null bei nicht-numerischen Werten.
// Unbekannte Keys werden verworfen — es erreicht nur validiertes JSON den Python-Spawn.
SortedDictionary<string, double>? ValidateParams(JsonElement schema, JsonElement? given)
{
    var result = new SortedDictionary<string, double>();
    foreach (var p in schema.EnumerateObject())
    {
        var v = p.Value.GetProperty("default").GetDouble();
        if (given?.ValueKind == JsonValueKind.Object &&
            given.Value.TryGetProperty(p.Name, out var raw))
        {
            if (raw.ValueKind != JsonValueKind.Number) return null;
            v = raw.GetDouble();
        }
        if (p.Value.TryGetProperty("min", out var min)) v = Math.Max(v, min.GetDouble());
        if (p.Value.TryGetProperty("max", out var max)) v = Math.Min(v, max.GetDouble());
        result[p.Name] = v;
    }
    return result;
}

// --------------------------------------------------------------------------
// Admin auth — ein Passwort (Config "AdminPassword", z. B. aus /etc/stocks/env),
// HMAC-signiertes Cookie mit Ablauf. Ohne konfiguriertes Passwort bleiben alle
// mutierenden Endpunkte gesperrt (View-only).
// --------------------------------------------------------------------------

var adminPassword = app.Configuration["AdminPassword"];
var adminKey = string.IsNullOrEmpty(adminPassword)
    ? null
    : SHA256.HashData(Encoding.UTF8.GetBytes("stocks-admin-v1:" + adminPassword));
if (adminKey is null)
    app.Logger.LogWarning("Kein AdminPassword konfiguriert — App läuft im reinen View-Modus.");

const string AdminCookie = "stocks_admin";

string MakeAdminToken()
{
    var exp = DateTimeOffset.UtcNow.AddDays(30).ToUnixTimeSeconds();
    var sig = Convert.ToHexString(HMACSHA256.HashData(adminKey!, Encoding.UTF8.GetBytes("admin:" + exp)));
    return exp + "." + sig;
}

bool IsAdmin(HttpRequest request)
{
    if (adminKey is null || request.Cookies[AdminCookie] is not string token) return false;
    var parts = token.Split('.');
    if (parts.Length != 2 || !long.TryParse(parts[0], out var exp)) return false;
    if (exp < DateTimeOffset.UtcNow.ToUnixTimeSeconds()) return false;
    var expected = Convert.ToHexString(HMACSHA256.HashData(adminKey, Encoding.UTF8.GetBytes("admin:" + parts[0])));
    return CryptographicOperations.FixedTimeEquals(
        Encoding.ASCII.GetBytes(expected), Encoding.ASCII.GetBytes(parts[1].ToUpperInvariant()));
}

app.MapGet("/api/me", (HttpRequest request) => Results.Json(new { admin = IsAdmin(request) }));

app.MapPost("/api/login", async (HttpRequest request, HttpResponse response) =>
{
    if (adminKey is null)
        return Results.Json(new { error = "kein AdminPassword konfiguriert" }, statusCode: 503);
    var body = await JsonSerializer.DeserializeAsync<JsonElement>(request.Body);
    var pw = body.TryGetProperty("password", out var p) ? p.GetString() : null;
    var ok = pw is not null && CryptographicOperations.FixedTimeEquals(
        SHA256.HashData(Encoding.UTF8.GetBytes(pw)),
        SHA256.HashData(Encoding.UTF8.GetBytes(adminPassword!)));
    if (!ok)
    {
        await Task.Delay(500); // stumpfes Bruteforce-Bremsen
        return Results.Unauthorized();
    }
    response.Cookies.Append(AdminCookie, MakeAdminToken(), new CookieOptions
    {
        HttpOnly = true,
        SameSite = SameSiteMode.Strict,
        MaxAge = TimeSpan.FromDays(30),
        Path = "/",
    });
    return Results.Json(new { admin = true });
});

app.MapPost("/api/logout", (HttpResponse response) =>
{
    response.Cookies.Delete(AdminCookie);
    return Results.Json(new { admin = false });
});

// --------------------------------------------------------------------------
// Yahoo symbol search (shared by autocomplete + watchlist validation)
// --------------------------------------------------------------------------

async Task<List<Dictionary<string, object?>>> YahooSearch(string query)
{
    var cache = app.Services.GetRequiredService<IMemoryCache>();
    var key = "search:" + query.ToLowerInvariant();
    if (cache.TryGetValue(key, out List<Dictionary<string, object?>>? cached) && cached is not null)
        return cached;

    var http = app.Services.GetRequiredService<IHttpClientFactory>().CreateClient("yahoo");
    var url = "https://query1.finance.yahoo.com/v1/finance/search" +
              $"?q={Uri.EscapeDataString(query)}&quotesCount=8&newsCount=0&listsCount=0";
    var results = new List<Dictionary<string, object?>>();
    try
    {
        using var doc = JsonDocument.Parse(await http.GetStringAsync(url));
        if (doc.RootElement.TryGetProperty("quotes", out var quotes))
            foreach (var q in quotes.EnumerateArray())
            {
                if (!q.TryGetProperty("symbol", out var symbol)) continue;
                results.Add(new Dictionary<string, object?>
                {
                    ["symbol"] = symbol.GetString(),
                    ["name"] = q.TryGetProperty("longname", out var ln) ? ln.GetString()
                             : q.TryGetProperty("shortname", out var sn) ? sn.GetString() : null,
                    ["exchange"] = q.TryGetProperty("exchDisp", out var ex) ? ex.GetString() : null,
                    ["type"] = q.TryGetProperty("quoteType", out var qt) ? qt.GetString() : null,
                });
            }
        cache.Set(key, results, TimeSpan.FromMinutes(15));
    }
    catch (Exception e)
    {
        app.Logger.LogWarning("Yahoo search failed for {Query}: {Error}", query, e.Message);
    }
    return results;
}

// --------------------------------------------------------------------------
// API
// --------------------------------------------------------------------------

app.MapGet("/api/watchlist", () =>
{
    var rows = Query("""
        SELECT w.symbol, w.name, w.holding, w.autotrade,
               w.strat_name, w.strat_timeframe,
               a.as_of, a.action, a.signal, a.strategy, a.pillar_total,
               a.trend_score, a.momentum_score, a.macro_score, a.flags_json,
               (SELECT close FROM bars b WHERE b.symbol = w.symbol ORDER BY date DESC LIMIT 1) AS last_price,
               (SELECT date  FROM bars b WHERE b.symbol = w.symbol ORDER BY date DESC LIMIT 1) AS last_date,
               (SELECT close FROM bars b WHERE b.symbol = w.symbol ORDER BY date DESC LIMIT 1 OFFSET 1) AS prev_price
        FROM watchlist w
        LEFT JOIN analysis a ON a.symbol = w.symbol AND a.timeframe = '1d'
            AND a.as_of = (SELECT MAX(as_of) FROM analysis
                           WHERE symbol = w.symbol AND timeframe = '1d')
        WHERE w.enabled = 1
        ORDER BY w.symbol
        """);
    foreach (var row in rows)
    {
        row["flags"] = ParseJson(row["flags_json"]);
        row.Remove("flags_json");
        row["change_pct"] =
            row["last_price"] is double lp && row["prev_price"] is double pp && pp != 0
                ? Math.Round((lp / pp - 1) * 100, 2) : null;
    }
    return Results.Json(rows);
});

app.MapGet("/api/symbols/{symbol}/bars", (string symbol, string? range, string? tf) =>
{
    var is1h = tf == "1h";
    // 1h-Zeiten sind ISO-UTC-Datetimes — Lightweight Charts erwartet für
    // Intraday-Daten Unix-Sekunden statt "yyyy-MM-dd"-Strings.
    long ToEpoch(string ts) => new DateTimeOffset(DateTime.ParseExact(
        ts, "yyyy-MM-dd'T'HH:mm:ss", System.Globalization.CultureInfo.InvariantCulture,
        System.Globalization.DateTimeStyles.AssumeUniversal |
        System.Globalization.DateTimeStyles.AdjustToUniversal)).ToUnixTimeSeconds();

    var rows = Query(
        is1h
            ? "SELECT ts AS date, open, high, low, close, volume FROM bars_1h " +
              "WHERE symbol = @s AND close IS NOT NULL ORDER BY ts"
            : "SELECT date, open, high, low, close, volume FROM bars " +
              "WHERE symbol = @s AND close IS NOT NULL ORDER BY date",
        ("@s", symbol.ToUpperInvariant()));
    if (rows.Count == 0) return Results.NotFound(new { error = "no bars for symbol" });

    var closes = rows.Select(r => Convert.ToDouble(r["close"])).ToArray();
    var e20 = EmaSeries(closes, 20);
    var e50 = EmaSeries(closes, 50);
    var e200 = EmaSeries(closes, 200);

    // EMAs are computed over the full cached history, then cut to the range,
    // so overlays are already correct at the left edge of the chart.
    // (Datums-Cutoff vergleicht auch gegen "yyyy-MM-ddTHH:mm:ss" korrekt ordinal.)
    var cutoff = (range?.ToLowerInvariant()) switch
    {
        "5d" => DateTime.UtcNow.AddDays(-5).ToString("yyyy-MM-dd"),
        "2w" => DateTime.UtcNow.AddDays(-14).ToString("yyyy-MM-dd"),
        "1m" => DateTime.UtcNow.AddMonths(-1).ToString("yyyy-MM-dd"),
        "3m" => DateTime.UtcNow.AddMonths(-3).ToString("yyyy-MM-dd"),
        "6m" => DateTime.UtcNow.AddMonths(-6).ToString("yyyy-MM-dd"),
        _ => "0000", // default: everything we cache
    };

    var bars = new List<object>();
    var ema20 = new List<object>();
    var ema50 = new List<object>();
    var ema200 = new List<object>();
    for (var i = 0; i < rows.Count; i++)
    {
        var date = (string)rows[i]["date"]!;
        if (string.CompareOrdinal(date, cutoff) < 0) continue;
        object time = is1h ? ToEpoch(date) : date;
        bars.Add(new
        {
            time,
            open = rows[i]["open"], high = rows[i]["high"],
            low = rows[i]["low"], close = rows[i]["close"],
            volume = rows[i]["volume"],
        });
        if (e20[i] is double v20) ema20.Add(new { time, value = Math.Round(v20, 4) });
        if (e50[i] is double v50) ema50.Add(new { time, value = Math.Round(v50, 4) });
        if (e200[i] is double v200) ema200.Add(new { time, value = Math.Round(v200, 4) });
    }
    return Results.Json(new { symbol = symbol.ToUpperInvariant(), timeframe = is1h ? "1h" : "1d", bars, ema20, ema50, ema200 });
});

app.MapGet("/api/symbols/{symbol}/analysis", (string symbol, string? tf) =>
{
    var rows = Query("""
        SELECT a.*, w.name, w.holding, w.autotrade,
               w.strat_name, w.strat_params, w.strat_timeframe, w.strat_risk FROM analysis a
        LEFT JOIN watchlist w ON w.symbol = a.symbol
        WHERE a.symbol = @s AND a.timeframe = @tf ORDER BY a.as_of DESC LIMIT 1
        """, ("@s", symbol.ToUpperInvariant()), ("@tf", tf == "1h" ? "1h" : "1d"));
    if (rows.Count == 0) return Results.NotFound(new { error = "no analysis yet" });
    var row = rows[0];
    row["flags"] = ParseJson(row["flags_json"]);
    row["indicators"] = ParseJson(row["indicators_json"]);
    row["strat_params"] = ParseJson(row["strat_params"]);
    row["strat_risk"] = ParseJson(row["strat_risk"]);
    row.Remove("flags_json");
    row.Remove("indicators_json");
    return Results.Json(row);
});

app.MapGet("/api/macro", () =>
{
    var rows = Query("SELECT * FROM macro_snapshot ORDER BY as_of DESC LIMIT 1");
    if (rows.Count == 0) return Results.NotFound(new { error = "no macro snapshot yet" });
    var row = rows[0];
    row["components"] = ParseJson(row["components_json"]);
    row["notes"] = ParseJson(row["notes_json"]);
    row.Remove("components_json");
    row.Remove("notes_json");
    return Results.Json(row);
});

// --------------------------------------------------------------------------
// Strategien + Backtesting
// --------------------------------------------------------------------------

app.MapGet("/api/strategies", async (string? tf) =>
{
    var timeframe = tf == "1h" ? "1h" : "1d";
    var catalog = await StrategyCatalog();
    if (catalog is null)
        return Results.Json(new { error = "Strategie-Liste nicht verfügbar (PythonPath prüfen)" }, statusCode: 503);
    EnsureStrategyTable();
    var saved = Query("SELECT name, params_json, active FROM strategy_config WHERE timeframe = @tf",
            ("@tf", timeframe))
        .ToDictionary(r => (string)r["name"]!);

    var rows = new List<Dictionary<string, object?>>();
    foreach (var s in catalog.Value.EnumerateArray())
    {
        var name = s.GetProperty("name").GetString()!;
        saved.TryGetValue(name, out var cfg);
        rows.Add(new Dictionary<string, object?>
        {
            ["name"] = name,
            ["label"] = s.GetProperty("label").GetString(),
            ["description"] = s.GetProperty("description").GetString(),
            ["params"] = s.GetProperty("params"),
            ["saved_params"] = cfg is null ? null : ParseJson(cfg["params_json"]),
            ["active"] = cfg is not null && Convert.ToInt64(cfg["active"]!) == 1,
        });
    }
    // Ohne explizite Wahl gilt der Drei-Säulen-Standard als aktiv (Worker-Fallback).
    if (rows.Count > 0 && !rows.Any(r => (bool)r["active"]!))
        (rows.FirstOrDefault(r => (string)r["name"]! == "three_pillars") ?? rows[0])["active"] = true;
    return Results.Json(rows);
});

app.MapPut("/api/strategies/{name}", async (string name, HttpRequest request) =>
{
    if (!IsAdmin(request)) return Results.Unauthorized();
    var catalog = await StrategyCatalog();
    if (catalog is null)
        return Results.Json(new { error = "Strategie-Liste nicht verfügbar (PythonPath prüfen)" }, statusCode: 503);
    if (FindStrategySchema(catalog.Value, name) is not JsonElement schema)
        return Results.NotFound(new { error = $"unbekannte Strategie '{name}'" });

    var body = await JsonSerializer.DeserializeAsync<JsonElement>(request.Body);
    var timeframe = body.TryGetProperty("timeframe", out var tfEl) && tfEl.GetString() == "1h"
        ? "1h" : "1d";
    string? paramsJson = null;
    if (body.TryGetProperty("params", out var given))
    {
        var validated = ValidateParams(schema, given);
        if (validated is null)
            return Results.BadRequest(new { error = "params: nur numerische Werte erlaubt" });
        paramsJson = JsonSerializer.Serialize(validated);
    }

    EnsureStrategyTable();
    long activeVal;
    using (var con = Open())
    using (var tx = con.BeginTransaction())
    {
        if (body.TryGetProperty("active", out var act) &&
            act.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            activeVal = act.GetBoolean() ? 1 : 0;
            if (activeVal == 1)
            {
                // "aktiv" gilt pro Timeframe — 1d- und 1h-Läufe haben je eine Strategie
                using var off = con.CreateCommand();
                off.Transaction = tx;
                off.CommandText = "UPDATE strategy_config SET active = 0 WHERE timeframe = @tf";
                off.Parameters.AddWithValue("@tf", timeframe);
                off.ExecuteNonQuery();
            }
        }
        else
        {
            using var cur = con.CreateCommand();
            cur.Transaction = tx;
            cur.CommandText = "SELECT active FROM strategy_config WHERE name = @n AND timeframe = @tf";
            cur.Parameters.AddWithValue("@n", name);
            cur.Parameters.AddWithValue("@tf", timeframe);
            activeVal = cur.ExecuteScalar() is long l ? l : 0;
        }
        using var up = con.CreateCommand();
        up.Transaction = tx;
        up.CommandText = """
            INSERT INTO strategy_config(name, timeframe, params_json, active) VALUES(@n, @tf, @p, @a)
            ON CONFLICT(name, timeframe) DO UPDATE SET
              params_json = COALESCE(excluded.params_json, params_json),
              active = excluded.active
            """;
        up.Parameters.AddWithValue("@n", name);
        up.Parameters.AddWithValue("@tf", timeframe);
        up.Parameters.AddWithValue("@p", (object?)paramsJson ?? DBNull.Value);
        up.Parameters.AddWithValue("@a", activeVal);
        up.ExecuteNonQuery();
        tx.Commit();
    }
    return Results.Json(new { name, timeframe, saved = paramsJson is not null, active = activeVal == 1 });
});

app.MapGet("/api/symbols/{symbol}/backtest", async (string symbol, string? strategy, string? @params, string? tf, string? risk, string? slippage) =>
{
    symbol = symbol.ToUpperInvariant();
    var timeframe = tf == "1h" ? "1h" : "1d";
    var (riskCanonical, riskError) = ValidateRisk(risk);
    if (riskError is not null)
        return Results.BadRequest(new { error = riskError });
    var (slipCanonical, slipError) = ValidateSlippage(slippage);
    if (slipError is not null)
        return Results.BadRequest(new { error = slipError });
    var catalog = await StrategyCatalog();
    if (catalog is null)
        return Results.Json(new { error = "Backtest nicht verfügbar (PythonPath prüfen)" }, statusCode: 503);
    strategy ??= "three_pillars";
    if (FindStrategySchema(catalog.Value, strategy) is not JsonElement schema)
        return Results.NotFound(new { error = $"unbekannte Strategie '{strategy}'" });

    JsonElement? given = null;
    if (!string.IsNullOrEmpty(@params))
    {
        try { given = JsonSerializer.Deserialize<JsonElement>(@params); }
        catch (JsonException) { return Results.BadRequest(new { error = "params: kein gültiges JSON" }); }
    }
    var validated = ValidateParams(schema, given);
    if (validated is null)
        return Results.BadRequest(new { error = "params: nur numerische Werte erlaubt" });
    var canonical = JsonSerializer.Serialize(validated);

    var last = Query(timeframe == "1h"
            ? "SELECT MAX(ts) d FROM bars_1h WHERE symbol = @s AND close IS NOT NULL"
            : "SELECT MAX(date) d FROM bars WHERE symbol = @s AND close IS NOT NULL",
        ("@s", symbol));
    if (last.Count == 0 || last[0]["d"] is not string lastDate)
        return Results.NotFound(new { error = "no bars for symbol" });

    var cache = app.Services.GetRequiredService<IMemoryCache>();
    var key = $"bt:{symbol}:{strategy}:{canonical}:{timeframe}:{riskCanonical}:{slipCanonical}:{lastDate}";
    if (cache.TryGetValue(key, out string? cached) && cached is not null)
        return Results.Content(cached, "application/json");

    var btArgs = new List<string> { "run", symbol,
        "--strategy", strategy, "--params", canonical, "--timeframe", timeframe,
        "--db", dbPath };
    if (riskCanonical is not null) { btArgs.Add("--risk"); btArgs.Add(riskCanonical); }
    if (slipCanonical is not null) { btArgs.Add("--slippage-bps"); btArgs.Add(slipCanonical); }
    var (code, stdout, stderr) = await RunPython(btArgs.ToArray());
    if (code != 0)
    {
        app.Logger.LogError("Backtest {Symbol}/{Strategy} fehlgeschlagen ({Code}): {Err}",
            symbol, strategy, code, string.IsNullOrEmpty(stderr) ? stdout : stderr);
        // backtest.py meldet fachliche Fehler als {"error": ...} auf stdout
        var payload = stdout.TrimStart().StartsWith('{')
            ? stdout
            : JsonSerializer.Serialize(new { error = "Backtest fehlgeschlagen" });
        return Results.Content(payload, "application/json", null, 422);
    }
    cache.Set(key, stdout, TimeSpan.FromHours(1));
    return Results.Content(stdout, "application/json");
});

// Portfolio-Backtest: Pott-Modell (wie trader.py) über die ganze Watchlist,
// eine Strategie-Konfiguration für alle Symbole. Läuft deutlich länger als
// ein Einzel-Backtest → eigenes Timeout.
app.MapGet("/api/portfolio/backtest", async (string? strategy, string? @params, string? tf, string? risk, string? slippage) =>
{
    var timeframe = tf == "1h" ? "1h" : "1d";
    var (riskCanonical, riskError) = ValidateRisk(risk);
    if (riskError is not null)
        return Results.BadRequest(new { error = riskError });
    var (slipCanonical, slipError) = ValidateSlippage(slippage);
    if (slipError is not null)
        return Results.BadRequest(new { error = slipError });
    var catalog = await StrategyCatalog();
    if (catalog is null)
        return Results.Json(new { error = "Backtest nicht verfügbar (PythonPath prüfen)" }, statusCode: 503);
    strategy ??= "three_pillars";
    if (FindStrategySchema(catalog.Value, strategy) is not JsonElement schema)
        return Results.NotFound(new { error = $"unbekannte Strategie '{strategy}'" });

    JsonElement? given = null;
    if (!string.IsNullOrEmpty(@params))
    {
        try { given = JsonSerializer.Deserialize<JsonElement>(@params); }
        catch (JsonException) { return Results.BadRequest(new { error = "params: kein gültiges JSON" }); }
    }
    var validated = ValidateParams(schema, given);
    if (validated is null)
        return Results.BadRequest(new { error = "params: nur numerische Werte erlaubt" });
    var canonical = JsonSerializer.Serialize(validated);

    var symbols = Query("SELECT symbol FROM watchlist ORDER BY symbol")
        .Select(r => (string)r["symbol"]!).ToList();
    if (symbols.Count == 0)
        return Results.NotFound(new { error = "Watchlist ist leer" });

    var last = Query(timeframe == "1h"
        ? "SELECT MAX(ts) d FROM bars_1h WHERE close IS NOT NULL"
        : "SELECT MAX(date) d FROM bars WHERE close IS NOT NULL");
    if (last.Count == 0 || last[0]["d"] is not string lastDate)
        return Results.NotFound(new { error = "keine Bars in der DB" });

    var cache = app.Services.GetRequiredService<IMemoryCache>();
    var key = $"pf:{string.Join(",", symbols)}:{strategy}:{canonical}:{timeframe}:{riskCanonical}:{slipCanonical}:{lastDate}";
    if (cache.TryGetValue(key, out string? cached) && cached is not null)
        return Results.Content(cached, "application/json");

    var pfArgs = new List<string> { "portfolio", "--symbols", string.Join(",", symbols),
        "--strategy", strategy, "--params", canonical, "--timeframe", timeframe,
        "--db", dbPath };
    if (riskCanonical is not null) { pfArgs.Add("--risk"); pfArgs.Add(riskCanonical); }
    if (slipCanonical is not null) { pfArgs.Add("--slippage-bps"); pfArgs.Add(slipCanonical); }
    // three_pillars über ~100 Symbole × 5 Jahre braucht ~2 min (O(n²) je Symbol)
    var (code, stdout, stderr) = await RunPython(pfArgs.ToArray(), timeoutSeconds: 300);
    if (code != 0)
    {
        app.Logger.LogError("Portfolio-Backtest {Strategy} fehlgeschlagen ({Code}): {Err}",
            strategy, code, string.IsNullOrEmpty(stderr) ? stdout : stderr);
        var payload = stdout.TrimStart().StartsWith('{')
            ? stdout
            : JsonSerializer.Serialize(new { error = "Portfolio-Backtest fehlgeschlagen" });
        return Results.Content(payload, "application/json", null, 422);
    }
    cache.Set(key, stdout, TimeSpan.FromHours(1));
    return Results.Content(stdout, "application/json");
});

app.MapGet("/api/symbols/search", async (string? q) =>
    string.IsNullOrWhiteSpace(q) || q.Trim().Length < 2
        ? Results.Json(Array.Empty<object>())
        : Results.Json(await YahooSearch(q.Trim())));

app.MapPost("/api/watchlist", async (HttpRequest request) =>
{
    if (!IsAdmin(request)) return Results.Unauthorized();
    var body = await JsonSerializer.DeserializeAsync<JsonElement>(request.Body);
    var symbol = body.TryGetProperty("symbol", out var s) ? s.GetString()?.Trim() : null;
    if (string.IsNullOrEmpty(symbol))
        return Results.BadRequest(new { error = "symbol required" });

    // Only accept symbols Yahoo actually knows — same source as the price fetch,
    // so anything accepted here is guaranteed fetchable by the worker.
    var matches = await YahooSearch(symbol);
    var exact = matches.FirstOrDefault(m =>
        string.Equals(m["symbol"] as string, symbol, StringComparison.OrdinalIgnoreCase));
    if (exact is null)
        return Results.UnprocessableEntity(new { error = $"'{symbol}' ist kein bekanntes Yahoo-Symbol" });

    var canonical = (string)exact["symbol"]!;
    Execute("""
        INSERT INTO watchlist(symbol, name, enabled, holding, added_at)
        VALUES(@s, @n, 1, 0, @t)
        ON CONFLICT(symbol) DO UPDATE SET enabled = 1, name = COALESCE(excluded.name, name)
        """,
        ("@s", canonical), ("@n", exact["name"]),
        ("@t", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")));
    return Results.Json(new { symbol = canonical, name = exact["name"], pending = true },
        statusCode: StatusCodes.Status201Created);
});

app.MapDelete("/api/watchlist/{symbol}", (string symbol, HttpRequest request) =>
{
    if (!IsAdmin(request)) return Results.Unauthorized();
    // Soft delete: bars/analysis history stays, symbol vanishes from UI + worker runs.
    var n = Execute("UPDATE watchlist SET enabled = 0 WHERE symbol = @s",
        ("@s", symbol.ToUpperInvariant()));
    return n > 0 ? Results.NoContent() : Results.NotFound();
});

app.MapPatch("/api/watchlist/{symbol}", async (string symbol, HttpRequest request) =>
{
    if (!IsAdmin(request)) return Results.Unauthorized();
    var body = await JsonSerializer.DeserializeAsync<JsonElement>(request.Body);
    var sets = new List<string>();
    var args = new List<(string, object?)> { ("@s", symbol.ToUpperInvariant()) };
    if (body.TryGetProperty("holding", out var h))
    {
        sets.Add("holding = @h");
        args.Add(("@h", h.GetBoolean() ? 1 : 0));
    }
    if (body.TryGetProperty("autotrade", out var at))
    {
        var on = at.GetBoolean();
        sets.Add("autotrade = @a");
        args.Add(("@a", on ? 1 : 0));
        if (on)
        {
            // Lock-in: die im UI gewählte Strategie (+Params +Timeframe) wird
            // eingefroren — Worker analysiert und Trader handelt das Symbol
            // fortan exakt mit dieser Konfiguration, bis Auto-Trade aus geht.
            var stratName = body.TryGetProperty("strategy", out var sEl) ? sEl.GetString() : null;
            if (string.IsNullOrEmpty(stratName))
                return Results.BadRequest(new { error = "autotrade an: strategy erforderlich" });
            var lockTf = body.TryGetProperty("timeframe", out var tEl) && tEl.GetString() == "1h"
                ? "1h" : "1d";
            var catalog = await StrategyCatalog();
            if (catalog is null)
                return Results.Json(new { error = "Strategie-Liste nicht verfügbar (PythonPath prüfen)" }, statusCode: 503);
            if (FindStrategySchema(catalog.Value, stratName) is not JsonElement schema)
                return Results.NotFound(new { error = $"unbekannte Strategie '{stratName}'" });
            JsonElement? givenParams = body.TryGetProperty("params", out var pEl) ? pEl : null;
            var validated = ValidateParams(schema, givenParams);
            if (validated is null)
                return Results.BadRequest(new { error = "params: nur numerische Werte erlaubt" });
            var (riskCanonical, riskError) = ValidateRisk(
                body.TryGetProperty("risk", out var rEl) ? rEl.GetRawText() : null);
            if (riskError is not null)
                return Results.BadRequest(new { error = riskError });
            sets.Add("strat_name = @sn");
            args.Add(("@sn", stratName));
            sets.Add("strat_params = @sp");
            args.Add(("@sp", JsonSerializer.Serialize(validated)));
            sets.Add("strat_timeframe = @st");
            args.Add(("@st", lockTf));
            sets.Add("strat_risk = @sr");
            args.Add(("@sr", (object?)riskCanonical ?? DBNull.Value));
        }
        else
        {
            sets.Add("strat_name = NULL");
            sets.Add("strat_params = NULL");
            sets.Add("strat_timeframe = NULL");
            sets.Add("strat_risk = NULL");
        }
    }
    if (sets.Count == 0)
        return Results.BadRequest(new { error = "holding oder autotrade erforderlich" });
    var n = Execute($"UPDATE watchlist SET {string.Join(", ", sets)} WHERE symbol = @s AND enabled = 1",
        args.ToArray());
    return n > 0 ? Results.NoContent() : Results.NotFound();
});

// Read-only Trading-Status: letzter Konto-Snapshot (vom Worker geschrieben),
// Pott-Größe und die letzten Orders. enabled spiegelt das TRADING_ENABLED-Opt-in.
app.MapGet("/api/trading", () =>
{
    var enabledRow = Query("SELECT value FROM meta WHERE key = 'trading_enabled'");
    var enabled = enabledRow.Count > 0 && (string?)enabledRow[0]["value"] == "1";

    var snapRows = Query("SELECT * FROM broker_snapshot ORDER BY as_of DESC LIMIT 1");
    Dictionary<string, object?>? snapshot = null;
    if (snapRows.Count > 0)
    {
        snapshot = snapRows[0];
        snapshot["positions"] = ParseJson(snapshot["positions_json"]);
        snapshot.Remove("positions_json");
    }

    var nAuto = Convert.ToInt64(Query(
        "SELECT COUNT(*) c FROM watchlist WHERE enabled = 1 AND autotrade = 1")[0]["c"]!);
    double? pot = null;
    if (snapshot?["equity"] is double eq && nAuto > 0)
        pot = Math.Round(eq / nAuto, 2);

    var orders = Query("SELECT * FROM orders ORDER BY submitted_at DESC LIMIT 50");
    return Results.Json(new Dictionary<string, object?>
    {
        ["enabled"] = enabled,
        ["snapshot"] = snapshot,
        ["autotrade_count"] = nAuto,
        ["pot"] = pot,
        ["orders"] = orders,
    });
});

// --------------------------------------------------------------------------
// Static frontend
// --------------------------------------------------------------------------

app.UseDefaultFiles();
app.UseStaticFiles();
app.MapGet("/symbol/{symbol}", (string symbol) =>
    Results.File(Path.Combine(app.Environment.WebRootPath!, "symbol.html"), "text/html"));

app.Run();
