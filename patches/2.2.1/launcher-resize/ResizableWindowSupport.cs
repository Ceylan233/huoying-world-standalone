using System;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;

namespace NyaLauncher.ResizeSupport;

public static class ResizableWindowSupport
{
    private const int WmNcHitTest = 0x0084;
    private const int HtClient = 1;
    private const int HtLeft = 10;
    private const int HtRight = 11;
    private const int HtTop = 12;
    private const int HtTopLeft = 13;
    private const int HtTopRight = 14;
    private const int HtBottom = 15;
    private const int HtBottomLeft = 16;
    private const int HtBottomRight = 17;
    private const int ResizeBorderDip = 8;

    private static readonly ConditionalWeakTable<Window, WindowHook> Hooks = new();

    public static void Attach(Window window)
    {
        if (window is null || Hooks.TryGetValue(window, out _))
        {
            return;
        }

        var hook = new WindowHook(window);
        Hooks.Add(window, hook);
        window.SourceInitialized += hook.OnSourceInitialized;
        window.Loaded += hook.OnLoaded;
        window.Closed += hook.OnClosed;
    }

    private sealed class WindowHook
    {
        private readonly Window _window;
        private HwndSource? _source;
        private bool _contentWrapped;

        public WindowHook(Window window)
        {
            _window = window;
        }

        public void OnSourceInitialized(object? sender, EventArgs e)
        {
            _window.ResizeMode = ResizeMode.CanResize;
            _source = PresentationSource.FromVisual(_window) as HwndSource;
            _source?.AddHook(WndProc);
        }

        public void OnLoaded(object sender, RoutedEventArgs e)
        {
            _window.SizeToContent = SizeToContent.Manual;
            _window.ResizeMode = ResizeMode.CanResize;
            _window.MinWidth = 760;
            _window.MinHeight = 560;
            _window.MaxWidth = double.PositiveInfinity;
            _window.MaxHeight = double.PositiveInfinity;
            WrapContentForScaling();
        }

        public void OnClosed(object? sender, EventArgs e)
        {
            if (_source is not null)
            {
                _source.RemoveHook(WndProc);
                _source = null;
            }
        }

        private void WrapContentForScaling()
        {
            if (_contentWrapped || _window.Content is not UIElement content)
            {
                return;
            }

            _window.Content = null;
            _window.Content = new Viewbox
            {
                Stretch = Stretch.Uniform,
                StretchDirection = StretchDirection.Both,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                VerticalAlignment = VerticalAlignment.Stretch,
                Child = content
            };
            _contentWrapped = true;
        }

        private IntPtr WndProc(
            IntPtr hwnd,
            int message,
            IntPtr wParam,
            IntPtr lParam,
            ref bool handled)
        {
            if (message != WmNcHitTest || _window.WindowState != WindowState.Normal)
            {
                return IntPtr.Zero;
            }

            if (!GetWindowRect(hwnd, out var rect))
            {
                return new IntPtr(HtClient);
            }

            long packed = lParam.ToInt64();
            int x = unchecked((short)(packed & 0xffff));
            int y = unchecked((short)((packed >> 16) & 0xffff));
            double dpiScale = _source?.CompositionTarget?.TransformToDevice.M11 ?? 1.0;
            int border = Math.Max(6, (int)Math.Round(ResizeBorderDip * dpiScale));

            bool left = x >= rect.Left && x < rect.Left + border;
            bool right = x <= rect.Right && x > rect.Right - border;
            bool top = y >= rect.Top && y < rect.Top + border;
            bool bottom = y <= rect.Bottom && y > rect.Bottom - border;

            int hit = HtClient;
            if (top && left) hit = HtTopLeft;
            else if (top && right) hit = HtTopRight;
            else if (bottom && left) hit = HtBottomLeft;
            else if (bottom && right) hit = HtBottomRight;
            else if (left) hit = HtLeft;
            else if (right) hit = HtRight;
            else if (top) hit = HtTop;
            else if (bottom) hit = HtBottom;

            if (hit != HtClient)
            {
                handled = true;
                return new IntPtr(hit);
            }

            return IntPtr.Zero;
        }
    }

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
}
