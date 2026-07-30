// Streamer.bot "Execute C# Code" sub-action
//
// Attach this to the same Action as your "Gnome Yap" Reward Redemption
// trigger. It POSTs {"username": ..., "message": ...} to the local
// HTTP listener in twitch_gnome_bot.py (run_redeem_server()).
//
// Notes:
//  - "user" and "rawInput" are the standard Streamer.bot argument names
//    for a Twitch reward redemption trigger (rawInput is the viewer's
//    typed text, if the reward requires text input -- if yours doesn't,
//    it'll just come through empty and the Python side falls back to a
//    default phrase).
//  - Uses a static HttpClient (best practice: avoids socket exhaustion
//    from creating a new one per redemption).
//  - Test with Streamer.bot's "Test Code" button first -- it'll run
//    Execute() with whatever test arg values you've set up, or empty/
//    default ones, so check CPH.LogInfo/LogError output in the
//    Streamer.bot log window afterward.

using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;

public class CPHInline
{
    private static readonly HttpClient httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };

    private const string GnomeYapUrl = "http://127.0.0.1:3939/gnome_yap";

    public bool Execute()
    {
        CPH.TryGetArg("user", out string username);
        CPH.TryGetArg("rawInput", out string message);

        username = string.IsNullOrWhiteSpace(username) ? "anonymous" : username;
        message = message ?? "";

        var payload = new { username = username, message = message };
        string json = JsonSerializer.Serialize(payload);

        try
        {
            var content = new StringContent(json, Encoding.UTF8, "application/json");
            var response = httpClient.PostAsync(GnomeYapUrl, content).GetAwaiter().GetResult();
            CPH.LogInfo($"[Gnome Yap] POST -> {(int)response.StatusCode} {response.StatusCode}");
        }
        catch (Exception ex)
        {
            // Most likely cause: twitch_gnome_bot.py isn't running yet,
            // so nothing is listening on port 3939.
            CPH.LogError($"[Gnome Yap] POST failed: {ex.Message}");
        }

        return true;
    }
}