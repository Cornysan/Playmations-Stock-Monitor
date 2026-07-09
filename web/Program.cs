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
        SELECT w.symbol, w.name, w.holding,
               a.as_of, a.action, a.pillar_total,
               a.trend_score, a.momentum_score, a.macro_score, a.flags_json,
               (SELECT close FROM bars b WHERE b.symbol = w.symbol ORDER BY date DESC LIMIT 1) AS last_price,
               (SELECT date  FROM bars b WHERE b.symbol = w.symbol ORDER BY date DESC LIMIT 1) AS last_date,
               (SELECT close FROM bars b WHERE b.symbol = w.symbol ORDER BY date DESC LIMIT 1 OFFSET 1) AS prev_price
        FROM watchlist w
        LEFT JOIN analysis a ON a.symbol = w.symbol
            AND a.as_of = (SELECT MAX(as_of) FROM analysis WHERE symbol = w.symbol)
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

app.MapGet("/api/symbols/{symbol}/bars", (string symbol, string? range) =>
{
    var rows = Query(
        "SELECT date, open, high, low, close, volume FROM bars " +
        "WHERE symbol = @s AND close IS NOT NULL ORDER BY date",
        ("@s", symbol.ToUpperInvariant()));
    if (rows.Count == 0) return Results.NotFound(new { error = "no bars for symbol" });

    var closes = rows.Select(r => Convert.ToDouble(r["close"])).ToArray();
    var e20 = EmaSeries(closes, 20);
    var e50 = EmaSeries(closes, 50);
    var e200 = EmaSeries(closes, 200);

    // EMAs are computed over the full cached history, then cut to the range,
    // so overlays are already correct at the left edge of the chart.
    var cutoff = (range?.ToLowerInvariant()) switch
    {
        "3m" => DateTime.UtcNow.AddMonths(-3).ToString("yyyy-MM-dd"),
        "6m" => DateTime.UtcNow.AddMonths(-6).ToString("yyyy-MM-dd"),
        _ => "0000", // default: everything we cache (~1 year)
    };

    var bars = new List<object>();
    var ema20 = new List<object>();
    var ema50 = new List<object>();
    var ema200 = new List<object>();
    for (var i = 0; i < rows.Count; i++)
    {
        var date = (string)rows[i]["date"]!;
        if (string.CompareOrdinal(date, cutoff) < 0) continue;
        bars.Add(new
        {
            time = date,
            open = rows[i]["open"], high = rows[i]["high"],
            low = rows[i]["low"], close = rows[i]["close"],
            volume = rows[i]["volume"],
        });
        if (e20[i] is double v20) ema20.Add(new { time = date, value = Math.Round(v20, 4) });
        if (e50[i] is double v50) ema50.Add(new { time = date, value = Math.Round(v50, 4) });
        if (e200[i] is double v200) ema200.Add(new { time = date, value = Math.Round(v200, 4) });
    }
    return Results.Json(new { symbol = symbol.ToUpperInvariant(), bars, ema20, ema50, ema200 });
});

app.MapGet("/api/symbols/{symbol}/analysis", (string symbol) =>
{
    var rows = Query("""
        SELECT a.*, w.name, w.holding FROM analysis a
        LEFT JOIN watchlist w ON w.symbol = a.symbol
        WHERE a.symbol = @s ORDER BY a.as_of DESC LIMIT 1
        """, ("@s", symbol.ToUpperInvariant()));
    if (rows.Count == 0) return Results.NotFound(new { error = "no analysis yet" });
    var row = rows[0];
    row["flags"] = ParseJson(row["flags_json"]);
    row["indicators"] = ParseJson(row["indicators_json"]);
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
    if (!body.TryGetProperty("holding", out var h))
        return Results.BadRequest(new { error = "holding required" });
    var n = Execute("UPDATE watchlist SET holding = @h WHERE symbol = @s AND enabled = 1",
        ("@h", h.GetBoolean() ? 1 : 0), ("@s", symbol.ToUpperInvariant()));
    return n > 0 ? Results.NoContent() : Results.NotFound();
});

// --------------------------------------------------------------------------
// Static frontend
// --------------------------------------------------------------------------

app.UseDefaultFiles();
app.UseStaticFiles();
app.MapGet("/symbol/{symbol}", (string symbol) =>
    Results.File(Path.Combine(app.Environment.WebRootPath!, "symbol.html"), "text/html"));

app.Run();
