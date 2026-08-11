using System;
using System.ComponentModel;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[Guid("D27CDB6D-AE6D-11CF-96B8-444553540000")]
[InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
internal interface IShockwaveFlashEvents
{
    [DispId(150)]
    void FSCommand(string command, string args);
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
internal sealed class FlashEventSink : IShockwaveFlashEvents
{
    private readonly FlashActiveXHost host;

    internal FlashEventSink(FlashActiveXHost host)
    {
        this.host = host;
    }

    public void FSCommand(string command, string args)
    {
        host.RaiseFSCommand(command, args);
    }
}

internal sealed class FlashActiveXHost : AxHost
{
    private ConnectionPointCookie connectionPoint;
    private FlashEventSink eventSink;

    internal FlashActiveXHost()
        : base("D27CDB6E-AE6D-11CF-96B8-444553540000")
    {
    }

    internal object ActiveXObject
    {
        get { return GetOcx(); }
    }

    internal event Action<string, string> FSCommandReceived;

    internal void RaiseFSCommand(string command, string args)
    {
        Action<string, string> handler = FSCommandReceived;
        if (handler != null)
        {
            handler(command ?? "", args ?? "");
        }
    }

    protected override void CreateSink()
    {
        base.CreateSink();
        eventSink = new FlashEventSink(this);
        connectionPoint = new ConnectionPointCookie(
            ActiveXObject,
            eventSink,
            typeof(IShockwaveFlashEvents)
        );
    }

    protected override void DetachSink()
    {
        if (connectionPoint != null)
        {
            connectionPoint.Disconnect();
            connectionPoint = null;
        }
        eventSink = null;
        base.DetachSink();
    }
}

internal static class NativeFlashHost
{
    private static string logPath = "";

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            MessageBox.Show("原生 Flash 启动参数无效。", "火影世界");
            return 2;
        }

        string configPath = Path.GetFullPath(args[0]);
        string[] lines;
        try
        {
            lines = File.ReadAllLines(configPath, Encoding.ASCII);
            File.Delete(configPath);
        }
        catch (Exception error)
        {
            MessageBox.Show("无法读取原生 Flash 启动配置：" + error.Message, "火影世界");
            return 3;
        }
        if (lines.Length < 3)
        {
            MessageBox.Show("原生 Flash 启动配置不完整。", "火影世界");
            return 4;
        }

        string movie;
        string flashVars;
        string title;
        try
        {
            movie = Decode(lines[0]);
            flashVars = Decode(lines[1]);
            title = Decode(lines[2]);
            if (lines.Length >= 4)
            {
                logPath = Decode(lines[3]);
            }
        }
        catch (Exception error)
        {
            MessageBox.Show("原生 Flash 启动配置无法解析：" + error.Message, "火影世界");
            return 5;
        }

        Log("Starting native host; movie=" + movie);

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        Form window = new Form();
        window.Text = string.IsNullOrWhiteSpace(title) ? "火影世界" : title;
        window.StartPosition = FormStartPosition.CenterScreen;
        window.WindowState = FormWindowState.Maximized;
        window.MinimumSize = new System.Drawing.Size(960, 600);
        window.BackColor = System.Drawing.Color.Black;

        FlashActiveXHost flash = new FlashActiveXHost();
        ((ISupportInitialize)flash).BeginInit();
        flash.Dock = DockStyle.Fill;
        window.Controls.Add(flash);
        ((ISupportInitialize)flash).EndInit();

        flash.FSCommandReceived += delegate(string command, string commandArgs)
        {
            Log("FSCommand received; command=" + command + "; args=" + commandArgs);
            if (!string.Equals(command, "nyaReconnect", StringComparison.OrdinalIgnoreCase))
            {
                return;
            }
            window.BeginInvoke((MethodInvoker)delegate
            {
                try
                {
                    object ocx = flash.ActiveXObject;
                    string separator = movie.IndexOf('?') >= 0 ? "&" : "?";
                    string reconnectMovie = movie + separator + "reconnect=" +
                        DateTime.UtcNow.Ticks.ToString();
                    Invoke(ocx, "Stop");
                    SetProperty(ocx, "FlashVars", flashVars);
                    Invoke(ocx, "LoadMovie", new object[] { 0, reconnectMovie });
                    Invoke(ocx, "Play");
                    Log("Native Flash movie reloaded after nyaReconnect");
                }
                catch (Exception error)
                {
                    Log("Native Flash reconnect failed: " + Unwrap(error).ToString());
                }
            });
        };

        window.Shown += delegate
        {
            try
            {
                Log("Window shown; creating Flash ActiveX control");
                flash.CreateControl();
                object ocx = flash.ActiveXObject;
                Log("Flash ActiveX control created");
                SetProperty(ocx, "AllowScriptAccess", "always");
                SetProperty(ocx, "AllowNetworking", "all");
                SetProperty(ocx, "Menu", false);
                SetProperty(ocx, "WMode", "window");
                SetProperty(ocx, "Quality2", "Medium");
                SetProperty(ocx, "FlashVars", flashVars);
                SetProperty(ocx, "Movie", movie);
                Invoke(ocx, "Play");
                Log("Movie assigned and Play invoked");
            }
            catch (Exception error)
            {
                Log("Flash startup failed: " + Unwrap(error).ToString());
                MessageBox.Show(
                    "原生 Flash Player 启动失败：" + Unwrap(error).Message,
                    "火影世界"
                );
                window.Close();
            }
        };
        window.FormClosed += delegate { Log("Native host window closed"); };

        Application.Run(window);
        return 0;
    }

    private static string Decode(string value)
    {
        return Encoding.UTF8.GetString(Convert.FromBase64String(value));
    }

    private static void SetProperty(object target, string name, object value)
    {
        target.GetType().InvokeMember(
            name,
            BindingFlags.SetProperty,
            null,
            target,
            new object[] { value }
        );
    }

    private static void Invoke(object target, string name)
    {
        Invoke(target, name, new object[0]);
    }

    private static void Invoke(object target, string name, object[] args)
    {
        target.GetType().InvokeMember(
            name,
            BindingFlags.InvokeMethod,
            null,
            target,
            args
        );
    }

    private static Exception Unwrap(Exception error)
    {
        return error is TargetInvocationException && error.InnerException != null
            ? error.InnerException
            : error;
    }

    private static void Log(string message)
    {
        if (string.IsNullOrWhiteSpace(logPath))
        {
            return;
        }
        try
        {
            File.AppendAllText(
                logPath,
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " | " + message + Environment.NewLine,
                Encoding.UTF8
            );
        }
        catch
        {
        }
    }
}
