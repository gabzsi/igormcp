// Native Windows URI handler. It never starts Python, MCP, or an automation server.
using System;
using System.Collections.Generic;
using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class IgorFigureLink
{
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int command);
    private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 60000000 };

    private static bool BringForward(Process process, string experiment)
    {
        for (int attempt = 0; attempt < 100 && !process.HasExited; attempt++)
        {
            IntPtr found = IntPtr.Zero;
            EnumWindows(delegate(IntPtr hwnd, IntPtr parameter) {
                uint pid;
                GetWindowThreadProcessId(hwnd, out pid);
                if (pid != process.Id) return true;
                var title = new StringBuilder(1024);
                GetWindowText(hwnd, title, title.Capacity);
                if (title.ToString().IndexOf(experiment, StringComparison.OrdinalIgnoreCase) >= 0)
                    found = hwnd;
                return true;
            }, IntPtr.Zero);
            if (found != IntPtr.Zero)
            {
                ShowWindow(found, 9);
                SetForegroundWindow(found);
                return true;
            }
            System.Threading.Thread.Sleep(50);
        }
        return false;
    }

    private static string RequiredString(Dictionary<string, object> record, string key)
    {
        object value;
        if (!record.TryGetValue(key, out value) || !(value is string))
            throw new InvalidOperationException("The report is missing " + key + ". Regenerate its native figure package.");
        return (string)value;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length < 2) throw new ArgumentException("Expected a registry directory and an Igor report URI.");
            string registry = Path.GetFullPath(args[0]);
            Uri uri;
            if (!Uri.TryCreate(args[1], UriKind.Absolute, out uri) || uri.Scheme != "igor-report" ||
                (uri.Host != "edit" && uri.Host != "refresh") || uri.Query.Length != 0 || uri.Fragment.Length != 0)
                throw new ArgumentException("Invalid Igor figure link.");
            string[] parts = uri.AbsolutePath.Trim('/').Split('/');
            if (parts.Length < 1 || !Regex.IsMatch(parts[0], "^[A-Za-z0-9_-]{32}$"))
                throw new ArgumentException("Invalid report registration.");
            string entryFile = Path.Combine(registry, parts[0] + ".json");
            if (!File.Exists(entryFile)) throw new FileNotFoundException("This report is not registered on this computer.");
            var entry = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(entryFile));
            if (uri.Host == "refresh")
            {
                if (parts.Length != 1) throw new ArgumentException("Invalid refresh link.");
                Process.Start(new ProcessStartInfo(RequiredString(entry, "path")) { UseShellExecute = true });
                return 0;
            }
            int index;
            if (parts.Length != 2 || !Int32.TryParse(parts[1], out index) || index < 0)
                throw new ArgumentException("Invalid figure index.");
            var data = (Dictionary<string, object>)entry["data"];
            var figures = (IList)data["figures"];
            if (index >= figures.Count) throw new ArgumentException("Unknown figure.");
            var figure = (Dictionary<string, object>)figures[index];
            string experiment = Path.GetFullPath(RequiredString(figure, "native_file"));
            string procedure = Path.GetFullPath(RequiredString(data, "native_preview_procedure"));
            string executable = Path.GetFullPath(RequiredString(data, "igor_executable"));
            string expectedRoot = Path.GetFullPath(RequiredString(data, "native_assets")).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!experiment.StartsWith(expectedRoot, StringComparison.OrdinalIgnoreCase) ||
                !procedure.StartsWith(expectedRoot, StringComparison.OrdinalIgnoreCase) ||
                !experiment.EndsWith(".pxp", StringComparison.OrdinalIgnoreCase) ||
                !procedure.EndsWith(".ipf", StringComparison.OrdinalIgnoreCase) ||
                !File.Exists(experiment) || !File.Exists(procedure))
                throw new FileNotFoundException("The single-figure files are missing. Keep the report and its figure folder together.");
            if (!File.Exists(executable) || !Regex.IsMatch(Path.GetFileName(executable), "^Igor(64)?\\.exe$", RegexOptions.IgnoreCase))
                throw new FileNotFoundException("The registered Igor executable is unavailable.");
            string stateFile = Path.Combine(registry, parts[0] + "-figure-" + index + ".running.json");
            if (args.Length == 3 && args[2] == "--validate")
            {
                File.WriteAllText(Path.Combine(registry, "native-validation.json"), Json.Serialize(new { ok = true, file = experiment, procedure = procedure, executable = executable }));
                return 0;
            }
            if (File.Exists(stateFile))
            {
                try
                {
                    var state = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(stateFile));
                    var existing = Process.GetProcessById(Convert.ToInt32(state["pid"]));
                    if (!existing.HasExited && existing.StartTime.ToUniversalTime().Ticks == Convert.ToInt64(state["startTicks"]) &&
                        String.Equals(existing.MainModule.FileName, executable, StringComparison.OrdinalIgnoreCase))
                    {
                        if (BringForward(existing, Path.GetFileNameWithoutExtension(experiment))) return 0;
                    }
                }
                catch (Exception) { /* A closed editor can be opened again normally. */ }
            }
            // /I starts an independent, normal Igor instance, not /Automation.
            // /N is intentionally never used: unsaved work is never discarded.
            object prepared;
            bool hooksPrepared = data.TryGetValue("native_hooks_prepared", out prepared) && Convert.ToBoolean(prepared);
            string arguments = "/I \"" + experiment + "\"" + (hooksPrepared ? "" : " \"" + procedure + "\"");
            var process = Process.Start(new ProcessStartInfo(executable, arguments) {
                UseShellExecute = true,
                WorkingDirectory = expectedRoot,
                WindowStyle = ProcessWindowStyle.Normal
            });
            if (process == null) throw new InvalidOperationException("Windows could not open the figure in Igor.");
            File.WriteAllText(stateFile, Json.Serialize(new { pid = process.Id, startTicks = process.StartTime.ToUniversalTime().Ticks, file = experiment }));
            BringForward(process, Path.GetFileNameWithoutExtension(experiment));
            // Exiting this launcher cannot close Igor: it owns no COM references.
            process.Dispose();
            return 0;
        }
        catch (Exception error)
        {
            if (args.Length == 3 && args[2] == "--validate")
            {
                File.WriteAllText(Path.Combine(args[0], "native-validation.json"), Json.Serialize(new { ok = false, error = error.ToString() }));
                return 1;
            }
            MessageBox.Show(error.Message, "Igor Figure Links", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
