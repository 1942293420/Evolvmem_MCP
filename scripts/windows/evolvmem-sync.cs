using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Threading;
using System.Web.Script.Serialization;

internal static class EvolvMemSync
{
    private const int MaxCapturedOutputCharacters = 65536;

    private sealed class RunStatus
    {
        internal string StartedUtc;
        internal string FinishedUtc;
        internal int? ProcessExitCode;
        internal string RunState;
        internal string Error;
        internal int? CapturedVersions;
        internal int? AcknowledgedVersions;
        internal int? PendingVersions;
        internal int? DiscoveredSessions;
        internal int? CaptureErrors;
        internal int? DiscoveryErrors;
        internal string WorkerStatus;
        internal string WorkerLastStartedUtc;
        internal string WorkerLastFinishedUtc;
    }

    private sealed class BoundedText
    {
        private readonly StringBuilder value = new StringBuilder();
        private readonly int limit;
        private bool truncated;

        internal BoundedText(int limit)
        {
            this.limit = limit;
        }

        internal void Append(string line)
        {
            if (line == null)
            {
                return;
            }

            lock (value)
            {
                int remaining = limit - value.Length;
                if (remaining <= 0)
                {
                    truncated = true;
                    return;
                }

                int length = Math.Min(remaining, line.Length);
                value.Append(line, 0, length);
                if (length < line.Length)
                {
                    truncated = true;
                    return;
                }

                if (value.Length < limit)
                {
                    value.Append('\n');
                }
                else
                {
                    truncated = true;
                }
            }
        }

        internal string Value
        {
            get
            {
                lock (value)
                {
                    return value.ToString();
                }
            }
        }

        internal bool IsTruncated
        {
            get
            {
                lock (value)
                {
                    return truncated;
                }
            }
        }
    }

    private static int Main()
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string statusPath = Path.Combine(directory, "launcher-status.json");
        RunStatus status = new RunStatus();
        status.StartedUtc = UtcNow();
        status.RunState = "starting";
        int launcherExitCode = 1;
        try
        {
            WriteStatus(statusPath, status);
            string workerScript = Path.Combine(directory, "evolvmem-codex.ps1");
            if (!File.Exists(workerScript))
            {
                status.ProcessExitCode = launcherExitCode;
                status.RunState = "failed";
                status.Error = "worker script was not found.";
                return launcherExitCode;
            }

            BoundedText standardOutput = new BoundedText(MaxCapturedOutputCharacters);
            BoundedText standardError = new BoundedText(MaxCapturedOutputCharacters);
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = WindowsPowerShellPath();
            startInfo.Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(workerScript) + " -Action worker";
            startInfo.WorkingDirectory = directory;
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.WindowStyle = ProcessWindowStyle.Hidden;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;

            using (Process process = new Process())
            using (ManualResetEvent outputClosed = new ManualResetEvent(false))
            using (ManualResetEvent errorClosed = new ManualResetEvent(false))
            {
                process.StartInfo = startInfo;
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
                {
                    if (args.Data == null) { outputClosed.Set(); }
                    else { standardOutput.Append(args.Data); }
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args)
                {
                    if (args.Data == null) { errorClosed.Set(); }
                    else { standardError.Append(args.Data); }
                };
                if (!process.Start())
                {
                    status.ProcessExitCode = launcherExitCode;
                    status.RunState = "failed";
                    status.Error = "worker process could not start.";
                    return launcherExitCode;
                }

                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                outputClosed.WaitOne();
                errorClosed.WaitOne();
                status.ProcessExitCode = process.ExitCode;
            }

            if (status.ProcessExitCode.GetValueOrDefault() != 0)
            {
                status.RunState = "failed";
                status.Error = "worker process failed.";
                return status.ProcessExitCode.GetValueOrDefault(launcherExitCode);
            }

            if (standardOutput.IsTruncated || !TryReadWorkerResult(standardOutput.Value, status))
            {
                status.RunState = "failed";
                if (status.WorkerStatus == "retry_pending")
                {
                    status.Error = "worker completed with retry pending.";
                }
                else
                {
                    status.Error = "worker result was invalid.";
                }
                return launcherExitCode;
            }

            status.RunState = "completed";
            launcherExitCode = 0;
            return launcherExitCode;
        }
        catch (Exception)
        {
            if (!status.ProcessExitCode.HasValue)
            {
                status.ProcessExitCode = launcherExitCode;
            }
            status.RunState = "failed";
            status.Error = "worker process could not run.";
            return launcherExitCode;
        }
        finally
        {
            status.FinishedUtc = UtcNow();
            if (status.RunState == "starting")
            {
                status.RunState = "failed";
                status.Error = "worker process could not run.";
            }

            try
            {
                WriteStatus(statusPath, status);
            }
            catch (Exception)
            {
                // Never emit child output or credentials while attempting to
                // report a launcher failure.
            }
        }
    }

    private static string WindowsPowerShellPath()
    {
        return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            "WindowsPowerShell", "v1.0", "powershell.exe");
    }

    private static string Quote(string path)
    {
        return "\"" + path + "\"";
    }

    private static bool TryReadWorkerResult(string output, RunStatus status)
    {
        if (String.IsNullOrEmpty(output))
        {
            return false;
        }

        Dictionary<string, object> result;
        try
        {
            object parsed = new JavaScriptSerializer().DeserializeObject(output.Trim());
            result = parsed as Dictionary<string, object>;
        }
        catch (Exception)
        {
            return false;
        }

        if (result == null || result.ContainsKey("error") || IsFalse(result, "healthy"))
        {
            return false;
        }

        object workerStatus;
        if (!result.TryGetValue("worker_status", out workerStatus) || !(workerStatus is string))
        {
            return false;
        }

        status.WorkerStatus = (string)workerStatus;
        if (!TryReadNonNegativeCount(result, "captured_versions", out status.CapturedVersions) ||
            !TryReadNonNegativeCount(result, "acknowledged_versions", out status.AcknowledgedVersions) ||
            !TryReadNonNegativeCount(result, "pending_versions", out status.PendingVersions) ||
            !TryReadNonNegativeCount(result, "discovered_sessions", out status.DiscoveredSessions) ||
            !TryReadNonNegativeCount(result, "capture_errors", out status.CaptureErrors) ||
            !TryReadNonNegativeCount(result, "discovery_errors", out status.DiscoveryErrors))
        {
            return false;
        }

        status.WorkerLastStartedUtc = ReadString(result, "last_started_utc");
        status.WorkerLastFinishedUtc = ReadString(result, "last_finished_utc");
        if (status.WorkerStatus == "idle" &&
            (status.PendingVersions.GetValueOrDefault() != 0 ||
             status.CaptureErrors.GetValueOrDefault() != 0 ||
             status.DiscoveryErrors.GetValueOrDefault() != 0))
        {
            return false;
        }

        return status.WorkerStatus == "idle";
    }

    private static bool TryReadNonNegativeCount(Dictionary<string, object> result, string propertyName, out int? count)
    {
        count = null;
        object raw;
        if (!result.TryGetValue(propertyName, out raw))
        {
            return false;
        }

        if (raw is int)
        {
            int value = (int)raw;
            if (value < 0) { return false; }
            count = value;
            return true;
        }

        if (raw is long)
        {
            long value = (long)raw;
            if (value < 0 || value > Int32.MaxValue) { return false; }
            count = (int)value;
            return true;
        }

        return false;
    }

    private static string ReadString(Dictionary<string, object> result, string propertyName)
    {
        object value;
        return result.TryGetValue(propertyName, out value) && value is string ? (string)value : null;
    }

    private static bool IsFalse(Dictionary<string, object> result, string propertyName)
    {
        object value;
        return result.TryGetValue(propertyName, out value) && value is bool && !(bool)value;
    }

    private static void WriteStatus(string path, RunStatus status)
    {
        string temporaryPath = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporaryPath, ToJson(status), new UTF8Encoding(false));
            if (File.Exists(path))
            {
                File.Replace(temporaryPath, path, null);
            }
            else
            {
                File.Move(temporaryPath, path);
            }
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    private static string ToJson(RunStatus status)
    {
        StringBuilder json = new StringBuilder();
        json.Append("{\"started_utc\":\"").Append(JsonString(status.StartedUtc)).Append("\"");
        if (status.FinishedUtc != null)
        {
            json.Append(",\"finished_utc\":\"").Append(JsonString(status.FinishedUtc)).Append("\"");
        }
        if (status.ProcessExitCode.HasValue)
        {
            json.Append(",\"process_exit_code\":").Append(status.ProcessExitCode.Value.ToString(CultureInfo.InvariantCulture));
        }
        json.Append(",\"run_state\":\"").Append(JsonString(status.RunState)).Append("\"");
        if (status.Error != null)
        {
            json.Append(",\"error\":\"").Append(JsonString(status.Error)).Append("\"");
        }
        AppendCount(json, "captured_versions", status.CapturedVersions);
        AppendCount(json, "acknowledged_versions", status.AcknowledgedVersions);
        AppendCount(json, "pending_versions", status.PendingVersions);
        AppendCount(json, "discovered_sessions", status.DiscoveredSessions);
        AppendCount(json, "capture_errors", status.CaptureErrors);
        AppendCount(json, "discovery_errors", status.DiscoveryErrors);
        AppendString(json, "worker_status", status.WorkerStatus);
        AppendString(json, "worker_last_started_utc", status.WorkerLastStartedUtc);
        AppendString(json, "worker_last_finished_utc", status.WorkerLastFinishedUtc);
        json.Append("}\r\n");
        return json.ToString();
    }

    private static void AppendCount(StringBuilder json, string name, int? count)
    {
        if (count.HasValue)
        {
            json.Append(",\"").Append(name).Append("\":").Append(count.Value.ToString(CultureInfo.InvariantCulture));
        }
    }

    private static void AppendString(StringBuilder json, string name, string value)
    {
        if (value != null)
        {
            json.Append(",\"").Append(name).Append("\":\"").Append(JsonString(value)).Append("\"");
        }
    }

    private static string JsonString(string value)
    {
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
    }

    private static string UtcNow()
    {
        return DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
    }
}
