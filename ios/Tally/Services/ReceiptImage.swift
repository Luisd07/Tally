import ImageIO
import UIKit

/// Turns a picked photo (HEIC, PNG, JPEG) or a document scan into JPEG bytes the server accepts.
enum ReceiptImage {
    nonisolated static let maxLongEdge: CGFloat = 2000

    /// JPEG data with the long edge capped at `maxLongEdge`, or nil when `data` isn't a decodable image.
    nonisolated static func jpeg(from data: Data, quality: CGFloat = 0.8) -> Data? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? CGFloat,
              let height = properties[kCGImagePropertyPixelHeight] as? CGFloat
        else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: min(max(width, height), maxLongEdge),
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return render(size: CGSize(width: image.width, height: image.height), quality: quality) { _ in
            UIImage(cgImage: image).draw(in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        }
    }

    /// One JPEG of scanned `pages` stacked top to bottom at the widest page's width, scaled down so the
    /// long edge is at most `maxLongEdge`. The server takes one image per receipt, so a multi-page scan
    /// becomes one tall image. Nil when there are no pages.
    nonisolated static func jpeg(stacking pages: [CGImage], quality: CGFloat = 0.8) -> Data? {
        let pages = pages.filter { $0.width > 0 && $0.height > 0 }
        guard let width = pages.map({ CGFloat($0.width) }).max() else { return nil }
        let heights = pages.map { CGFloat($0.height) * width / CGFloat($0.width) }
        let scale = min(1, maxLongEdge / max(width, heights.reduce(0, +)))
        let size = CGSize(width: (width * scale).rounded(), height: (heights.reduce(0, +) * scale).rounded())
        return render(size: size, quality: quality) { _ in
            var top: CGFloat = 0
            for (page, height) in zip(pages, heights) {
                UIImage(cgImage: page).draw(in: CGRect(x: 0, y: top * scale, width: width * scale, height: height * scale))
                top += height
            }
        }
    }

    /// JPEG bytes of `size` pixels drawn by `draw` over white (so transparency doesn't turn black).
    private nonisolated static func render(
        size: CGSize, quality: CGFloat, draw: (UIGraphicsImageRendererContext) -> Void
    ) -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: size, format: format).jpegData(withCompressionQuality: quality) { context in
            UIColor.white.setFill()
            context.fill(CGRect(origin: .zero, size: size))
            draw(context)
        }
    }
}
