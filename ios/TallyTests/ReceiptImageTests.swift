import Testing
import UIKit
@testable import Tally

struct ReceiptImageTests {
    private func png(width: Int, height: Int) -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).pngData { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }
    }

    private func longEdge(of jpeg: Data) -> CGFloat? {
        UIImage(data: jpeg).map { max($0.size.width * $0.scale, $0.size.height * $0.scale) }
    }

    @Test func largePNGBecomesCappedJPEG() throws {
        let jpeg = try #require(ReceiptImage.jpeg(from: png(width: 3000, height: 1500)))
        #expect(Array(jpeg.prefix(2)) == [0xFF, 0xD8])
        #expect(try #require(longEdge(of: jpeg)) <= 2000)
    }

    @Test func smallImageIsNotUpscaled() throws {
        let jpeg = try #require(ReceiptImage.jpeg(from: png(width: 300, height: 200)))
        #expect(Array(jpeg.prefix(2)) == [0xFF, 0xD8])
        #expect(longEdge(of: jpeg) == 300)
    }

    @Test func undecodableDataReturnsNil() {
        #expect(ReceiptImage.jpeg(from: Data("not an image".utf8)) == nil)
    }

    // MARK: Stacking scanned pages

    private func page(width: Int, height: Int, color: UIColor) -> CGImage {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).image { context in
            color.setFill()
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }.cgImage!
    }

    private func pixelSize(of jpeg: Data) -> CGSize? {
        UIImage(data: jpeg)?.cgImage.map { CGSize(width: $0.width, height: $0.height) }
    }

    /// The red channel (0...255) at `point` of the decoded JPEG.
    private func red(of jpeg: Data, at point: CGPoint) -> Int? {
        guard let image = UIImage(data: jpeg)?.cgImage else { return nil }
        var pixel = [UInt8](repeating: 0, count: 4)
        let drawn: Bool = pixel.withUnsafeMutableBytes { buffer in
            guard let context = CGContext(
                data: buffer.baseAddress, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return false }
            // Shift the image so `point` (top-left origin) lands on the 1x1 context's only pixel.
            context.draw(image, in: CGRect(x: -point.x, y: point.y - CGFloat(image.height) + 1,
                                           width: CGFloat(image.width), height: CGFloat(image.height)))
            return true
        }
        return drawn ? Int(pixel[0]) : nil
    }

    @Test func onePageScanKeepsItsSize() throws {
        let jpeg = try #require(ReceiptImage.jpeg(stacking: [page(width: 600, height: 900, color: .red)]))
        #expect(Array(jpeg.prefix(2)) == [0xFF, 0xD8])
        #expect(pixelSize(of: jpeg) == CGSize(width: 600, height: 900))
    }

    @Test func pagesStackTopToBottomAtTheWidestPagesWidth() throws {
        let top = page(width: 400, height: 300, color: .red)
        let bottom = page(width: 200, height: 100, color: .blue)
        let jpeg = try #require(ReceiptImage.jpeg(stacking: [top, bottom]))
        // The narrow page scales up to 400 wide, so 100 tall becomes 200.
        #expect(pixelSize(of: jpeg) == CGSize(width: 400, height: 500))
        #expect(try #require(red(of: jpeg, at: CGPoint(x: 200, y: 100))) > 200)
        #expect(try #require(red(of: jpeg, at: CGPoint(x: 200, y: 450))) < 60)
    }

    @Test func tallStackIsCappedAtTheLongEdge() throws {
        let pages = (0..<3).map { _ in page(width: 1500, height: 2000, color: .red) }
        let jpeg = try #require(ReceiptImage.jpeg(stacking: pages))
        let size = try #require(pixelSize(of: jpeg))
        #expect(size.height == 2000)
        #expect(size.width == 500)
    }

    @Test func noPagesGivesNil() {
        #expect(ReceiptImage.jpeg(stacking: []) == nil)
    }
}
