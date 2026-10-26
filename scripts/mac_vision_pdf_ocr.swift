import AppKit
import Foundation
import PDFKit
import Vision

struct OCRPage: Encodable {
    let page: Int
    let text: String
}

struct DryRunResult: Encodable {
    let pageCount: Int
    let dryRun: Bool
}

let usage = "Usage: swift mac_vision_pdf_ocr.swift [--dry-run] <input.pdf>\nExtract selectable page text with PDFKit and scanned page text with on-device Apple Vision."
let args = Array(CommandLine.arguments.dropFirst())
if args.contains("--help") || args.contains("-h") {
    print(usage)
    exit(0)
}
let dryRun = args.contains("--dry-run")
let inputPath = args.first { !$0.hasPrefix("-") }
guard let inputPath else {
    fputs(usage + "\n", stderr)
    exit(2)
}
let inputURL = URL(fileURLWithPath: inputPath)
guard let document = PDFDocument(url: inputURL) else {
    fputs("Unable to open PDF: \(inputPath)\n", stderr)
    exit(3)
}
guard document.pageCount <= 1000 else {
    fputs("PDFs over 1,000 pages are not supported.\n", stderr)
    exit(4)
}
if dryRun {
    let result = DryRunResult(pageCount: document.pageCount, dryRun: true)
    let data = try JSONEncoder().encode(result)
    print(String(data: data, encoding: .utf8) ?? "{}")
    exit(0)
}

var results: [OCRPage] = []
for pageIndex in 0..<document.pageCount {
    guard let page = document.page(at: pageIndex) else {
        results.append(OCRPage(page: pageIndex + 1, text: ""))
        continue
    }
    let bounds = page.bounds(for: .mediaBox)
    let longestSide = max(bounds.width, bounds.height)
    let scale = longestSide > 0 ? min(2400.0 / longestSide, 4.0) : 2.0
    let size = CGSize(width: bounds.width * scale, height: bounds.height * scale)
    let thumbnail = page.thumbnail(of: size, for: .mediaBox)
    guard let image = thumbnail.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        results.append(OCRPage(page: pageIndex + 1, text: ""))
        continue
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.usesCPUOnly = true
    if #available(macOS 13.0, *) {
        request.automaticallyDetectsLanguage = true
    } else {
        let preferred = ["en-US", "el-GR", "de-DE", "fr-FR", "it-IT", "es-ES"]
        let supported = try request.supportedRecognitionLanguages()
        request.recognitionLanguages = preferred.filter { supported.contains($0) }
    }
    do {
        try VNImageRequestHandler(cgImage: image).perform([request])
        let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
        results.append(OCRPage(page: pageIndex + 1, text: text))
    } catch {
        fputs("Vision OCR failed on page \(pageIndex + 1): \(error.localizedDescription)\n", stderr)
        exit(5)
    }
}

do {
    let data = try JSONEncoder().encode(results)
    print(String(data: data, encoding: .utf8) ?? "[]")
} catch {
    fputs("Could not encode OCR results: \(error.localizedDescription)\n", stderr)
    exit(6)
}
