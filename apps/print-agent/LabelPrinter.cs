using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Printing;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

public sealed class LabelData
{
    public string Model { get; set; }
    public string System { get; set; }
    public string Storage { get; set; }
    public string Battery { get; set; }
    public string Imei { get; set; }
    public string Price { get; set; }
    public string StockCode { get; set; }
    public string QrPath { get; set; }
}

public static class LabelPrinter
{
    // 实际标签沿出纸方向为30mm，横向宽度为40mm：203dpi约320×240点。
    private const int PixelWidth = 320;
    private const int PixelHeight = 240;

    private static string Value(string value, string fallback = "-")
    {
        return String.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
    }

    private static string StorageText(string value)
    {
        value = Value(value);
        if (value != "-" && Char.IsDigit(value[value.Length - 1])) return value + "GB";
        return value;
    }

    private static Font MakeFont(float size, FontStyle style)
    {
        try { return new Font("Microsoft YaHei", size, style, GraphicsUnit.Pixel); }
        catch { return new Font(FontFamily.GenericSansSerif, size, style, GraphicsUnit.Pixel); }
    }

    private static GraphicsPath RoundedRectangle(Rectangle rectangle, int radius)
    {
        GraphicsPath path = new GraphicsPath();
        int diameter = radius * 2;
        path.AddArc(rectangle.Left, rectangle.Top, diameter, diameter, 180, 90);
        path.AddArc(rectangle.Right - diameter, rectangle.Top, diameter, diameter, 270, 90);
        path.AddArc(rectangle.Right - diameter, rectangle.Bottom - diameter, diameter, diameter, 0, 90);
        path.AddArc(rectangle.Left, rectangle.Bottom - diameter, diameter, diameter, 90, 90);
        path.CloseFigure();
        return path;
    }

    public static Bitmap Render(LabelData label)
    {
        Bitmap bitmap = new Bitmap(PixelWidth, PixelHeight, PixelFormat.Format24bppRgb);
        bitmap.SetResolution(203, 203);
        using (Graphics graphics = Graphics.FromImage(bitmap))
        using (Pen border = new Pen(Color.Black, 2))
        using (Font normal = MakeFont(16, FontStyle.Regular))
        using (Font model = MakeFont(16, FontStyle.Regular))
        using (Font serial = MakeFont(15, FontStyle.Regular))
        using (Font price = MakeFont(18, FontStyle.Bold))
        using (Brush brush = new SolidBrush(Color.Black))
        using (StringFormat format = new StringFormat(StringFormat.GenericTypographic))
        {
            graphics.Clear(Color.White);
            graphics.TextRenderingHint = System.Drawing.Text.TextRenderingHint.SingleBitPerPixelGridFit;

            string[] lines = {
                "型号:" + Value(label.Model),
                "系统:" + Value(label.System),
                "内存:" + StorageText(label.Storage),
                "电池:" + Value(label.Battery),
                "串号:" + Value(label.Imei),
                "特价:" + Value(label.Price)
            };

            int left = 12;
            format.FormatFlags = StringFormatFlags.NoWrap | StringFormatFlags.NoClip;
            format.Trimming = StringTrimming.EllipsisCharacter;
            int top = 10;
            int lineHeight = 37;
            for (int index = 0; index < lines.Length; index++)
            {
                Font selected = index == 0 ? model : index == 4 ? serial : index == lines.Length - 1 ? price : normal;
                float textWidth = index >= 4 ? 220 : 296;
                graphics.DrawString(lines[index], selected, brush, new RectangleF(left, top + index * lineHeight, textWidth, 29), format);
            }
            if (!String.IsNullOrWhiteSpace(label.QrPath) && File.Exists(label.QrPath))
            {
                using (Image qr = Image.FromFile(label.QrPath))
                {
                    graphics.InterpolationMode = InterpolationMode.NearestNeighbor;
                    graphics.DrawImage(qr, new Rectangle(238, 160, 76, 76));
                }
            }
        }
        return bitmap;
    }

    private static void Print(Bitmap bitmap, string printerName, string documentName)
    {
        using (PrintDocument document = new PrintDocument())
        {
            document.PrinterSettings.PrinterName = printerName;
            if (!document.PrinterSettings.IsValid) throw new InvalidOperationException("找不到打印机：" + printerName);

            document.DocumentName = documentName;
            document.PrintController = new StandardPrintController();
            PaperSize custom = new PaperSize("40mm x 30mm", 157, 118);
            custom.RawKind = 0;
            document.DefaultPageSettings.PaperSize = custom;
            document.DefaultPageSettings.Landscape = false;
            document.DefaultPageSettings.Margins = new Margins(0, 0, 0, 0);
            document.OriginAtMargins = false;

            document.PrintPage += delegate(object sender, PrintPageEventArgs args)
            {
                args.Graphics.PageUnit = GraphicsUnit.Display;
                args.Graphics.InterpolationMode = InterpolationMode.NearestNeighbor;
                args.Graphics.PixelOffsetMode = PixelOffsetMode.Half;
                args.Graphics.DrawImage(bitmap, new RectangleF(0, 0, 157, 118));
                args.HasMorePages = false;
            };
            document.Print();
        }
    }

    public static int Main(string[] args)
    {
        try
        {
            string payload = null, preview = null, printer = "NIIMBOT B1";
            bool shouldPrint = false;
            for (int index = 0; index < args.Length; index++)
            {
                if (args[index] == "--payload" && index + 1 < args.Length) payload = args[++index];
                else if (args[index] == "--preview" && index + 1 < args.Length) preview = args[++index];
                else if (args[index] == "--printer" && index + 1 < args.Length) printer = args[++index];
                else if (args[index] == "--print") shouldPrint = true;
            }
            if (String.IsNullOrWhiteSpace(payload)) throw new ArgumentException("缺少标签数据");

            string json = Encoding.UTF8.GetString(Convert.FromBase64String(payload));
            LabelData label = new JavaScriptSerializer().Deserialize<LabelData>(json);
            using (Bitmap bitmap = Render(label))
            {
                if (!String.IsNullOrWhiteSpace(preview))
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(preview)));
                    bitmap.Save(preview, ImageFormat.Png);
                }
                if (shouldPrint) Print(bitmap, printer, "掌柜台-" + Value(label.StockCode, "标签"));
            }
            Console.OutputEncoding = Encoding.UTF8;
            Console.WriteLine("PRINT_OK|" + printer + "|40x30-landscape|" + Value(label.StockCode));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.ToString());
            return 1;
        }
    }
}
