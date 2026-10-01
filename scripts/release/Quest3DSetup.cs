// Single-file, per-user installer bootstrap. No policy or firewall changes.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("Sterevi Setup")]
[assembly: AssemblyDescription("Sterevi per-user installation bootstrap")]
[assembly: AssemblyCompany("Sterevi contributors")]
[assembly: AssemblyProduct("Sterevi")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]

internal static class Quest3DSetup
{
    // Retained in the binary so independent audits reject damaged overlay footers.
    internal const string BootstrapMarker = "Q3D_SETUP_BOOTSTRAP_V1";
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("Q3DSETUPZIPv1\0\0\0");
    private const int FooterSize = 64;
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024, RecursionLimit = 100 };
    private static readonly HashSet<string> PrivateNames = new HashSet<string>(new string[] { "credentials.json", "web-access.clixml", "app_state.cfg", "host_state.cfg", "config.ini", "desktop.json", "receipt.json", "process.json", "launch.json", "sunshine.conf", "sunshine_state.json", "sunshine.log", ".env" }, StringComparer.OrdinalIgnoreCase);
    private static readonly HashSet<string> PrivateSuffixes = new HashSet<string>(new string[] { ".pem", ".key", ".keystore", ".jks", ".pfx", ".p12", ".clixml", ".log", ".jsonl", ".pyc", ".mp4", ".mkv", ".jpg", ".jpeg", ".wav", ".mp3", ".flac", ".webm", ".lnk", ".pdb" }, StringComparer.OrdinalIgnoreCase);
    private static readonly HashSet<string> PrivateDirectories = new HashSet<string>(new string[] { ".git", "__pycache__", ".godot", ".cache", "user-data", "signing", "captures", "screenshots" }, StringComparer.OrdinalIgnoreCase);
    private sealed class SetupException : Exception { internal readonly string Code; internal SetupException(string code, string message) : base(message) { Code = code; } }
    private sealed class Options
    {
        internal string Mode = "ui", Extract, Install, Python;
        internal bool Update, NoShortcuts;
        internal static Options Parse(string[] args)
        {
            Options result = new Options(); bool selected = false;
            for (int i = 0; i < args.Length; ++i)
            {
                string value = args[i];
                if (value == "--verify-only" || value == "--ui-self-test" || value == "--extract-only" || value == "--install")
                {
                    if (selected) throw Error("arguments", "하나의 실행 모드를 선택해 주세요");
                    selected = true; result.Mode = value.Substring(2);
                    if (value == "--extract-only" || value == "--install")
                    {
                        if (++i >= args.Length) throw Error("arguments", "대상 경로가 필요합니다");
                        if (value == "--extract-only") result.Extract = args[i]; else result.Install = args[i];
                    }
                }
                else if (value == "--python") { if (++i >= args.Length) throw Error("arguments", "Python 실행 파일이 필요합니다"); result.Python = args[i]; }
                else if (value == "--update") result.Update = true;
                else if (value == "--no-shortcuts") result.NoShortcuts = true;
                else throw Error("arguments", "지원하지 않는 실행 옵션입니다");
            }
            if (result.Mode != "install" && (result.Python != null || result.Update || result.NoShortcuts)) throw Error("arguments", "설치 모드 전용 옵션입니다");
            if (result.Mode == "install" && BuildInfo.Target != "pc") throw Error("arguments", "Quest 설치는 USB 설치 창에서 진행해 주세요");
            return result;
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        // Apply only to this process; never modify a Windows registry or policy.
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        Options options = null;
        try
        {
            options = Options.Parse(args);
            if (!Environment.Is64BitOperatingSystem) throw Error("platform", "64비트 Windows가 필요합니다");
            string sid = WindowsIdentity.GetCurrent().User.Value;
            using (Mutex mutex = new Mutex(false, "Local\\Quest3D.Setup." + sid))
            {
                bool held = false;
                try { held = mutex.WaitOne(0); } catch (AbandonedMutexException) { held = true; }
                if (!held) throw Error("busy", "다른 Sterevi 설치 창이 열려 있습니다");
                try
                {
                    if (options.Mode == "ui") return ShowProgress(options);
                    Dictionary<string, object> report = Run(options, null);
                    Console.WriteLine(Json.Serialize(report));
                    return (int)report["exit_code"];
                }
                finally { mutex.ReleaseMutex(); }
            }
        }
        catch (Exception failure)
        {
            SetupException known = failure as SetupException;
            string code = known != null ? known.Code : "setup_failed";
            string message = known != null ? known.Message : "설치 준비 실패. 파일을 다시 다운로드하거나 쓰기 가능한 드라이브에서 실행해 주세요";
            Console.WriteLine(Json.Serialize(new Dictionary<string, object> { { "bootstrap", BootstrapMarker }, { "passed", false }, { "error", code }, { "exception_kind", failure.GetType().Name }, { "exit_code", 1 } }));
            if (args.Length == 0) MessageBox.Show(message, "Sterevi Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static int ShowProgress(Options options)
    {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        using (Form form = new Form())
        {
            form.Text = "Sterevi Setup"; form.ClientSize = new Size(490, 145); form.StartPosition = FormStartPosition.CenterScreen;
            form.FormBorderStyle = FormBorderStyle.FixedDialog; form.MaximizeBox = false; form.MinimizeBox = false;
            form.BackColor = Color.FromArgb(24, 26, 31); form.ForeColor = Color.FromArgb(236, 239, 244); form.Font = new Font("Segoe UI", 10);
            Label title = new Label { Text = BuildInfo.Target == "pc" ? "Sterevi Desktop" : "Sterevi Quest", AutoSize = true, Location = new Point(28, 21), Font = new Font("Segoe UI", 16, FontStyle.Bold) };
            Label detail = new Label { Text = "설치 파일 확인", AutoSize = true, Location = new Point(30, 68) };
            ProgressBar progress = new ProgressBar { Location = new Point(30, 103), Size = new Size(430, 6), Style = ProgressBarStyle.Marquee };
            form.Controls.Add(title); form.Controls.Add(detail); form.Controls.Add(progress);
            bool finished = false; int resultCode = 1;
            form.FormClosing += delegate(object sender, FormClosingEventArgs e) { if (!finished) e.Cancel = true; };
            form.Shown += async delegate
            {
                try
                {
                    Dictionary<string, object> result = await Task.Run(delegate { return Run(options, delegate(string message) { if (!form.IsDisposed) form.BeginInvoke((Action)delegate { detail.Text = message; if (message == "설치 창 실행") form.Hide(); }); }); });
                    resultCode = (int)result["exit_code"];
                }
                catch (Exception error)
                {
                    SetupException known = error as SetupException;
                    MessageBox.Show(known != null ? known.Message : "설치 준비 실패. 파일을 다시 다운로드하거나 쓰기 가능한 드라이브에서 실행해 주세요", "Sterevi Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                finally { finished = true; form.Close(); }
            };
            Application.Run(form); return resultCode;
        }
    }

    private static Dictionary<string, object> Run(Options options, Action<string> status)
    {
        string ownedStage = null; bool clean = false;
        Dictionary<string, object> report = new Dictionary<string, object> { { "bootstrap", BootstrapMarker }, { "target", BuildInfo.Target }, { "version", BuildInfo.Version }, { "installed_confirmed", false } };
        try
        {
            string exe = Assembly.GetExecutingAssembly().Location;
            AssertUnlinked(exe);
            using (FileStream file = new FileStream(exe, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                long stubSize = VerifyOverlay(file);
                report["payload_sha256"] = BuildInfo.PayloadSha256; report["payload_bytes"] = BuildInfo.PayloadLength;
                using (BoundedStream slice = new BoundedStream(file, stubSize, BuildInfo.PayloadLength))
                using (ZipArchive archive = new ZipArchive(slice, ZipArchiveMode.Read, true))
                {
                    Dictionary<string, FileSpec> files = VerifyArchive(archive);
                    report["verified_files"] = files.Count;
                    if (options.Mode == "verify-only") { report["passed"] = true; report["exit_code"] = 0; return report; }
                    string stage;
                    if (options.Mode == "extract-only") stage = PrepareEmptyDestination(options.Extract);
                    else { stage = CreateStage(exe); ownedStage = stage; }
                    if (status != null) status("설치 파일 준비");
                    Extract(archive, files, stage);
                    report["extraction_verified"] = true;
                    if (options.Mode == "extract-only") { report["passed"] = true; report["exit_code"] = 0; return report; }
                    int code;
                    if (status != null) status("설치 창 실행");
                    if (options.Mode == "install")
                    {
                        string script = CheckedPath(stage, "scripts/release/install.ps1");
                        List<string> values = new List<string> { "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", script, "-Destination", options.Install };
                        if (options.Python != null) { values.Add("-Python"); values.Add(options.Python); }
                        if (options.Update) values.Add("-Update"); if (options.NoShortcuts) values.Add("-NoShortcuts");
                        using (Mutex installer = new Mutex(false, "Local\\Quest3D.Installer." + WindowsIdentity.GetCurrent().User.Value))
                        {
                            bool held = false; try { held = installer.WaitOne(0); } catch (AbandonedMutexException) { held = true; }
                            if (!held) throw Error("busy", "다른 Sterevi 설치 창이 열려 있습니다");
                            try { code = Child(stage, values); } finally { installer.ReleaseMutex(); }
                        }
                        report["installed_confirmed"] = code == 0;
                    }
                    else
                    {
                        List<string> values = new List<string> { "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", CheckedPath(stage, "scripts/release/installer-launcher.ps1"), "-Target", BuildInfo.Target };
                        if (options.Mode == "ui-self-test") { values.Add("-SelfTest"); values.Add("-NoDialog"); }
                        code = Child(stage, values); report["ui_closed"] = options.Mode != "ui-self-test"; report["ui_self_test"] = options.Mode == "ui-self-test";
                    }
                    report["child_exit_code"] = code; report["exit_code"] = code; report["passed"] = code == 0;
                    clean = code == 0;
                    if (code != 0 && options.Mode == "ui") throw Error("child_failed", "설치 창을 실행하지 못했습니다. 수정된 파일 없이 공식 설치 파일로 다시 실행해 주세요");
                }
            }
            return report;
        }
        finally
        {
            if (ownedStage != null && clean) report["staging_cleaned"] = CleanOwnedStage(ownedStage);
            else if (ownedStage != null) report["staging_preserved"] = true;
        }
    }

    private static long VerifyOverlay(FileStream stream)
    {
        if (stream.Length < FooterSize + 6) throw Error("integrity", "설치 파일 손상. 다시 다운로드해 주세요");
        byte[] mz = new byte[2]; ReadExactly(stream, mz); if (mz[0] != 77 || mz[1] != 90) throw Error("integrity", "설치 파일 형식 오류");
        stream.Position = stream.Length - FooterSize;
        byte[] footer = new byte[FooterSize]; ReadExactly(stream, footer);
        if (!footer.Take(16).SequenceEqual(Magic)) throw Error("integrity", "설치 파일 손상. 다시 다운로드해 주세요");
        ulong start = BitConverter.ToUInt64(footer, 16), size = BitConverter.ToUInt64(footer, 24);
        if (start < 2 || start > (ulong)stream.Length || size != (ulong)BuildInfo.PayloadLength || size > (ulong)stream.Length || start + size != (ulong)(stream.Length - FooterSize)) throw Error("integrity", "설치 파일 범위 오류");
        if (Hex(footer.Skip(32).ToArray()) != BuildInfo.PayloadSha256) throw Error("integrity", "설치 파일 식별 오류");
        stream.Position = (long)start; byte[] pk = new byte[4]; ReadExactly(stream, pk);
        if (!pk.SequenceEqual(new byte[] { 80, 75, 3, 4 })) throw Error("integrity", "설치 패키지 형식 오류");
        using (BoundedStream slice = new BoundedStream(stream, (long)start, (long)size)) using (SHA256 hash = SHA256.Create())
            if (Hex(hash.ComputeHash(slice)) != BuildInfo.PayloadSha256) throw Error("integrity", "설치 파일 확인 실패. 다시 다운로드해 주세요");
        return (long)start;
    }

    private sealed class FileSpec { internal long Size; internal string Sha; }
    private static Dictionary<string, FileSpec> VerifyArchive(ZipArchive archive)
    {
        Dictionary<string, ZipArchiveEntry> entries = new Dictionary<string, ZipArchiveEntry>(StringComparer.Ordinal);
        HashSet<string> folded = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (ZipArchiveEntry entry in archive.Entries)
        {
            SafeName(entry.FullName);
            int unixMode = (entry.ExternalAttributes >> 16) & 0xF000;
            if (!folded.Add(entry.FullName) || entry.Name.Length == 0 || (unixMode != 0 && unixMode != 0x8000) || (entry.ExternalAttributes & (0x400 | 0x10)) != 0) throw Error("archive", "설치 패키지 경로 오류");
            entries.Add(entry.FullName, entry);
        }
        foreach (string name in folded)
        {
            string[] parts = name.Split('/');
            for (int index = 1; index < parts.Length; ++index) if (folded.Contains(String.Join("/", parts.Take(index)))) throw Error("archive", "중첩된 설치 경로 오류");
        }
        ZipArchiveEntry manifest;
        if (!entries.TryGetValue("distribution-manifest.json", out manifest) || manifest.Length > 16 * 1024 * 1024) throw Error("manifest", "설치 패키지 정보 누락");
        Dictionary<string, object> document;
        using (StreamReader reader = new StreamReader(manifest.Open(), new UTF8Encoding(false, true))) document = Json.DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
        if (document == null || !document.ContainsKey("schema") || Convert.ToInt32(document["schema"], CultureInfo.InvariantCulture) != 1 || !document.ContainsKey("release") || (string)document["release"] != BuildInfo.Version) throw Error("manifest", "설치 버전 정보 오류");
        Dictionary<string, object> metadata = document.ContainsKey("metadata") ? document["metadata"] as Dictionary<string, object> : null;
        string kind = metadata != null && metadata.ContainsKey("kind") ? metadata["kind"] as string : null;
        if (kind == null || !kind.StartsWith(BuildInfo.Target + "-", StringComparison.Ordinal)) throw Error("manifest", "설치 대상 정보 오류");
        Dictionary<string, object> raw = document.ContainsKey("files") ? document["files"] as Dictionary<string, object> : null;
        if (raw == null || raw.Count == 0 || entries.Count != raw.Count + 1 || raw.ContainsKey("distribution-manifest.json")) throw Error("manifest", "설치 파일 목록 오류");
        Dictionary<string, FileSpec> result = new Dictionary<string, FileSpec>(StringComparer.Ordinal); folded.Clear(); long total = 0;
        foreach (KeyValuePair<string, object> pair in raw)
        {
            SafeName(pair.Key); if (!folded.Add(pair.Key)) throw Error("manifest", "중복된 설치 경로");
            Dictionary<string, object> value = pair.Value as Dictionary<string, object>; ZipArchiveEntry entry;
            if (value == null || !value.ContainsKey("bytes") || !value.ContainsKey("sha256") || !entries.TryGetValue(pair.Key, out entry)) throw Error("manifest", "설치 파일 누락");
            long size = Convert.ToInt64(value["bytes"], CultureInfo.InvariantCulture); string sha = value["sha256"] as string;
            if (size < 0 || size != entry.Length || sha == null || !Regex.IsMatch(sha, "^[a-f0-9]{64}$")) throw Error("manifest", "설치 파일 정보 오류");
            total += size; if (total > 8L * 1024 * 1024 * 1024) throw Error("archive", "지원 범위를 초과한 설치 패키지");
            using (Stream input = entry.Open()) using (SHA256 hash = SHA256.Create()) if (Hex(hash.ComputeHash(input)) != sha) throw Error("integrity", "설치 파일 확인 실패");
            result.Add(pair.Key, new FileSpec { Size = size, Sha = sha });
        }
        string[] needed = new string[] { "scripts/release/installer-launcher.ps1", BuildInfo.Target == "pc" ? "scripts/release/install-ui.ps1" : "scripts/release/quest-install-ui.ps1", BuildInfo.Target == "pc" ? "scripts/release/install.ps1" : "scripts/release/install-quest.ps1" };
        if (needed.Any(name => !result.ContainsKey(name))) throw Error("manifest", "설치 창 파일 누락");
        return result;
    }

    private static void Extract(ZipArchive archive, Dictionary<string, FileSpec> specs, string root)
    {
        foreach (ZipArchiveEntry entry in archive.Entries)
        {
            string destination = CheckedPath(root, entry.FullName);
            string directory = Path.GetDirectoryName(destination); AssertUnlinked(directory); Directory.CreateDirectory(directory); AssertUnlinked(directory);
            using (Stream input = entry.Open()) using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None)) input.CopyTo(output);
            AssertUnlinked(destination);
            FileSpec spec;
            if (specs.TryGetValue(entry.FullName, out spec)) using (FileStream output = File.OpenRead(destination)) using (SHA256 hash = SHA256.Create())
                if (output.Length != spec.Size || Hex(hash.ComputeHash(output)) != spec.Sha) throw Error("integrity", "추출된 설치 파일 확인 실패");
        }
    }

    private static string SafeName(string name)
    {
        if (String.IsNullOrWhiteSpace(name) || name.StartsWith("/", StringComparison.Ordinal) || name.IndexOfAny(new char[] { '\\', ':', '\0' }) >= 0) throw Error("path", "잘못된 설치 경로");
        string[] parts = name.Split('/');
        foreach (string part in parts)
            if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(" ", StringComparison.Ordinal) || part.EndsWith(".", StringComparison.Ordinal) || part.Any(c => c < 32 || "<>\"|?*".IndexOf(c) >= 0) || Regex.IsMatch(part, "^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\\..*)?$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant) || PrivateDirectories.Contains(part) || part.StartsWith(".venv", StringComparison.OrdinalIgnoreCase)) throw Error("path", "잘못된 설치 경로");
        if (PrivateNames.Contains(parts[parts.Length - 1]) || PrivateSuffixes.Contains(Path.GetExtension(parts[parts.Length - 1]))) throw Error("path", "개인 설정 파일이 포함된 설치 패키지");
        return name;
    }

    private static string CheckedPath(string root, string name)
    {
        SafeName(name); string full = Path.GetFullPath(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(root.TrimEnd('\\', '/') + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw Error("path", "설치 경로 범위 오류");
        AssertUnlinked(full); return full;
    }
    private static void AssertUnlinked(string path)
    {
        string current = Path.GetFullPath(path);
        while (!String.IsNullOrEmpty(current))
        {
            try { if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) throw Error("linked_path", "연결된 폴더 대신 일반 폴더를 선택해 주세요"); }
            catch (FileNotFoundException) { } catch (DirectoryNotFoundException) { }
            string parent = Path.GetDirectoryName(current.TrimEnd('\\', '/')); if (parent == current) break; current = parent;
        }
    }
    private static string PrepareEmptyDestination(string path)
    {
        string full = Path.GetFullPath(path); AssertUnlinked(full);
        if (full == Path.GetPathRoot(full) || File.Exists(full) || (Directory.Exists(full) && Directory.EnumerateFileSystemEntries(full).Any())) throw Error("destination", "빈 전용 폴더를 선택해 주세요");
        Directory.CreateDirectory(full); AssertUnlinked(full); return full.TrimEnd('\\', '/');
    }
    private static string CreateStage(string exe)
    {
        string[] bases = new string[] { Path.GetDirectoryName(exe), Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData) };
        foreach (string parent in bases.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            string stage = null;
            try
            {
                if (String.IsNullOrEmpty(parent) || !Directory.Exists(parent)) continue;
                AssertUnlinked(parent); stage = Path.Combine(parent, "Quest3D-Setup-" + Guid.NewGuid().ToString("N"));
                if (Directory.Exists(stage) || File.Exists(stage)) continue;
                DirectorySecurity security = new DirectorySecurity();
                security.SetAccessRuleProtection(true, false);
                SecurityIdentifier owner = WindowsIdentity.GetCurrent().User;
                security.SetOwner(owner);
                foreach (SecurityIdentifier identity in new SecurityIdentifier[] { owner, new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null), new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null) })
                    security.AddAccessRule(new FileSystemAccessRule(identity, FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
                Directory.CreateDirectory(stage, security); AssertUnlinked(stage); if (Directory.EnumerateFileSystemEntries(stage).Any()) throw Error("staging", "임시 폴더 충돌");
                using (FileStream marker = new FileStream(Path.Combine(stage, ".quest3d-stage-owner"), FileMode.CreateNew, FileAccess.Write, FileShare.None))
                { byte[] bytes = Encoding.ASCII.GetBytes(BootstrapMarker); marker.Write(bytes, 0, bytes.Length); }
                return stage;
            }
            catch (IOException) { } catch (UnauthorizedAccessException) { } catch (SetupException) { }
            // Any uncertain partially-created stage is deliberately preserved.
        }
        throw Error("staging", "설치 파일을 준비할 공간이 없습니다. 쓰기 가능한 드라이브에서 실행해 주세요");
    }
    private static bool CleanOwnedStage(string stage)
    {
        try
        {
            string name = Path.GetFileName(stage);
            if (!Regex.IsMatch(name, "^Quest3D-Setup-[0-9a-f]{32}$")) return false;
            AssertUnlinked(stage); string marker = Path.Combine(stage, ".quest3d-stage-owner"); AssertUnlinked(marker);
            if (!File.Exists(marker) || File.ReadAllText(marker, Encoding.ASCII) != BootstrapMarker) return false;
            List<string> files = new List<string>(), directories = new List<string>();
            WalkStage(stage, stage, files, directories);
            foreach (string file in files) { AssertUnlinked(file); File.Delete(file); }
            foreach (string directory in directories.OrderByDescending(path => path.Length)) { AssertUnlinked(directory); Directory.Delete(directory, false); }
            Directory.Delete(stage, false); return true;
        }
        catch { return false; }
    }
    private static void WalkStage(string root, string current, List<string> files, List<string> directories)
    {
        AssertUnlinked(current);
        foreach (string path in Directory.EnumerateFileSystemEntries(current))
        {
            string full = Path.GetFullPath(path);
            if (!full.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw Error("cleanup", "임시 폴더 범위 오류");
            AssertUnlinked(full);
            if ((File.GetAttributes(full) & FileAttributes.Directory) != 0) { WalkStage(root, full, files, directories); directories.Add(full); } else files.Add(full);
        }
    }
    private static int Child(string stage, List<string> arguments)
    {
        string shell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"); AssertUnlinked(shell);
        ProcessStartInfo start = new ProcessStartInfo(shell, String.Join(" ", arguments.Select(Quote))) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden, WorkingDirectory = stage, RedirectStandardOutput = true, RedirectStandardError = true };
        start.EnvironmentVariables["POWERSHELL_TELEMETRY_OPTOUT"] = "true";
        using (Process process = new Process())
        {
            process.StartInfo = start; if (!process.Start()) throw Error("launch", "설치 창 실행 실패");
            Task<string> output = process.StandardOutput.ReadToEndAsync(), errors = process.StandardError.ReadToEndAsync();
            process.WaitForExit(); Task.WaitAll(output, errors);
            if (process.ExitCode != 0)
            {
                // Detailed paths remain in the private owned stage for local diagnosis.
                using (FileStream log = new FileStream(Path.Combine(stage, ".quest3d-child.stdout.log"), FileMode.CreateNew, FileAccess.Write, FileShare.None)) { byte[] data = Encoding.UTF8.GetBytes(output.Result); log.Write(data, 0, data.Length); }
                using (FileStream log = new FileStream(Path.Combine(stage, ".quest3d-child.stderr.log"), FileMode.CreateNew, FileAccess.Write, FileShare.None)) { byte[] data = Encoding.UTF8.GetBytes(errors.Result); log.Write(data, 0, data.Length); }
            }
            return process.ExitCode;
        }
    }
    private static string Quote(string value)
    {
        if (value == null || value.IndexOfAny(new char[] { '\0', '\r', '\n' }) >= 0) throw Error("arguments", "잘못된 실행 경로");
        StringBuilder result = new StringBuilder("\""); int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { ++slashes; continue; }
            if (character == '"') { result.Append('\\', slashes * 2 + 1); result.Append('"'); slashes = 0; continue; }
            result.Append('\\', slashes); slashes = 0; result.Append(character);
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }
    private static string Hex(byte[] data) { return BitConverter.ToString(data).Replace("-", "").ToLowerInvariant(); }
    private static void ReadExactly(Stream stream, byte[] data) { int offset = 0; while (offset < data.Length) { int size = stream.Read(data, offset, data.Length - offset); if (size == 0) throw Error("integrity", "설치 파일 잘림"); offset += size; } }
    private static SetupException Error(string code, string message) { return new SetupException(code, message); }

    private sealed class BoundedStream : Stream
    {
        private readonly Stream stream; private readonly long offset, length; private long position;
        internal BoundedStream(Stream stream, long offset, long length) { this.stream = stream; this.offset = offset; this.length = length; }
        public override bool CanRead { get { return true; } } public override bool CanWrite { get { return false; } } public override bool CanSeek { get { return true; } }
        public override long Length { get { return length; } } public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }
        public override int Read(byte[] buffer, int start, int count) { stream.Position = offset + position; int size = stream.Read(buffer, start, (int)Math.Min(count, length - position)); position += size; return size; }
        public override long Seek(long value, SeekOrigin origin) { long next = origin == SeekOrigin.Begin ? value : origin == SeekOrigin.Current ? position + value : length + value; if (next < 0 || next > length) throw new IOException("Bounded stream seek"); position = next; return position; }
        public override void Flush() { } public override void SetLength(long value) { throw new NotSupportedException(); } public override void Write(byte[] buffer, int start, int count) { throw new NotSupportedException(); }
    }
}
