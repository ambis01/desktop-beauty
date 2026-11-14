using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

// desktop beauty 的转发器：任务栏那个图标点一下就跑到这里，
// 它只做一件事——把信号丢给常驻的 python 进程，然后立刻退出（~30ms）。
//
// 参数：
//   toggle  开/关（默认）
//   panel   打开面板
//   quit    退出
// 按住 Shift 再点图标 = 打开面板（系统不让给任务栏图标加自定义右键项，
// 只能这样留一个入口）。
//
// 常驻进程不在的时候（被结束、崩了、刚开机还没起）不能只是静静退出，
// 否则点了图标一点反应都没有——这里直接把 app 拉起来。
//
// 目标从 exe 同目录的 target.txt 读（安装器会写）：
//   第 1 行 = 程序本体。打包版是 app\desktopbeauty-app.exe；源码版是 pythonw.exe
//   第 2 行 = 工作目录。打包版是 app\；源码版是项目目录（里面要有 run.pyw）
// 相对路径按 exe 所在目录解析。没有 target.txt 时，只认打包版布局
// （同目录下的 app\desktopbeauty-app.exe）——源码版必须先写 target.txt，
// 这里不再写死任何人的本机路径。
class Launcher
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr OpenEvent(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetEvent(IntPtr handle);
    [DllImport("user32.dll")]
    static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    const string BundledApp = @"app\desktopbeauty-app.exe";

    static string BaseDir
    {
        get { return AppDomain.CurrentDomain.BaseDirectory; }
    }

    static string Resolve(string path, string basis)
    {
        if (path == null || path.Trim().Length == 0) return null;
        path = path.Trim();
        if (Path.IsPathRooted(path)) return path;
        return Path.GetFullPath(Path.Combine(basis, path));
    }

    static bool IsPython(string exe)
    {
        string n = Path.GetFileName(exe).ToLowerInvariant();
        return n == "pythonw.exe" || n == "python.exe";
    }

    static void ReadTarget(out string exe, out string dir)
    {
        exe = null;
        dir = null;
        try
        {
            string p = Path.Combine(BaseDir, "target.txt");
            if (File.Exists(p))
            {
                string[] lines = File.ReadAllLines(p);
                if (lines.Length >= 1) exe = Resolve(lines[0], BaseDir);
                if (lines.Length >= 2) dir = Resolve(lines[1], BaseDir);
            }
        }
        catch { }

        // 没有 target.txt（或者第一行是空的）：只认打包版那个固定位置
        if (exe == null)
        {
            string bundled = Path.Combine(BaseDir, BundledApp);
            if (File.Exists(bundled))
            {
                exe = bundled;
                dir = Path.GetDirectoryName(bundled);
            }
            else
            {
                ShowMissing();
            }
        }
        if (dir == null || !Directory.Exists(dir)) dir = BaseDir;
    }

    static void ShowMissing()
    {
        try
        {
            MessageBoxW(IntPtr.Zero,
                "找不到程序本体。\r\n\r\n" +
                "请把 target.txt 放在 desktopbeauty.exe 同目录：\r\n" +
                "第 1 行 = 程序（app\\desktopbeauty-app.exe 或 pythonw.exe）\r\n" +
                "第 2 行 = 工作目录（打包版是 app，源码版是项目目录）",
                "desktop beauty", 0x10);
        }
        catch { }
    }

    static bool StartApp(bool panel)
    {
        string exe, dir;
        ReadTarget(out exe, out dir);
        if (exe == null || !File.Exists(exe)) return false;

        string file, args, cwd;
        if (IsPython(exe))
        {
            string script = Path.Combine(dir, "run.pyw");
            if (!File.Exists(script)) return false;
            file = exe;
            args = "\"" + script + "\"" + (panel ? " --panel" : "");
            cwd = dir;
        }
        else
        {
            file = exe;
            args = panel ? "--panel" : "";
            cwd = dir;
        }
        try
        {
            var psi = new ProcessStartInfo(file);
            psi.Arguments = args;
            psi.WorkingDirectory = cwd;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            Process.Start(psi);
            return true;
        }
        catch { return false; }
    }

    [STAThread]
    static int Main(string[] args)
    {
        string name = args.Length > 0 ? args[0] : "toggle";
        if ((GetAsyncKeyState(0x10) & 0x8000) != 0) name = "panel";   // VK_SHIFT

        IntPtr h = OpenEvent(0x0002, false, @"Local\desktopbeauty." + name);
        if (h != IntPtr.Zero)
        {
            SetEvent(h);
            return 0;
        }

        // 没在跑：quit 没有意义，其余一律拉起来。
        if (name == "quit") return 1;
        return StartApp(name == "panel") ? 0 : 1;
    }
}
