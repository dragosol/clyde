import SwiftUI

struct ChatBubbleShape: Shape {
    let isSent: Bool
    let hasTail: Bool
    let isGroupTop: Bool
    let isGroupMiddle: Bool
    let isGroupBottom: Bool

    private let pillR: CGFloat = 22       // full radius for single-line (pill shape)
    private let squircleR: CGFloat = 18    // tighter radius for multi-line (squircle)
    private let smallR: CGFloat = 6
    private let tailH: CGFloat = 7
    private let tailShift: CGFloat = 13

    func path(in rect: CGRect) -> Path {
        let W = rect.width
        let H = rect.height
        let bodyH = hasTail ? H - tailH : H

        // Adaptive radius: pill for single-line, squircle for multi-line
        let singleLineMax: CGFloat = 46
        let baseR = bodyH > singleLineMax ? squircleR : pillR

        // Corner radii
        var rtl = baseR, rtr = baseR, rbl = baseR, rbr = baseR

        if isGroupTop    { if isSent { rbr = smallR } else { rbl = smallR } }
        if isGroupMiddle { if isSent { rtr = smallR; rbr = smallR } else { rtl = smallR; rbl = smallR } }
        if isGroupBottom { if isSent { rtr = smallR } else { rtl = smallR } }

        // Clamp radii to half the smaller dimension
        let maxR = min(W, bodyH) / 2
        rtl = min(rtl, maxR); rtr = min(rtr, maxR)
        rbl = min(rbl, maxR); rbr = min(rbr, maxR)

        var path = Path()

        // Start at top-left corner
        path.move(to: CGPoint(x: rtl, y: 0))

        // Top edge
        path.addLine(to: CGPoint(x: W - rtr, y: 0))

        // TR corner
        path.addArc(
            center: CGPoint(x: W - rtr, y: rtr),
            radius: rtr,
            startAngle: .degrees(-90),
            endAngle: .degrees(0),
            clockwise: false
        )

        // Right edge
        if rtr + rbr < bodyH {
            path.addLine(to: CGPoint(x: W, y: bodyH - rbr))
        }

        if hasTail && isSent {
            buildSentTail(path: &path, W: W, bodyH: bodyH, rbr: rbr, rbl: rbl)
        } else if hasTail && !isSent {
            buildReceivedTail(path: &path, W: W, bodyH: bodyH, rbr: rbr, rbl: rbl, rtl: rtl)
        } else {
            // Normal BR corner
            path.addArc(
                center: CGPoint(x: W - rbr, y: bodyH - rbr),
                radius: rbr,
                startAngle: .degrees(0),
                endAngle: .degrees(90),
                clockwise: false
            )
            path.addLine(to: CGPoint(x: rbl, y: bodyH))
        }

        if !(hasTail && !isSent) {
            // BL corner
            path.addArc(
                center: CGPoint(x: rbl, y: bodyH - rbl),
                radius: rbl,
                startAngle: .degrees(90),
                endAngle: .degrees(180),
                clockwise: false
            )

            // Left edge
            if rtl + rbl < bodyH {
                path.addLine(to: CGPoint(x: 0, y: rtl))
            }

            // TL corner
            path.addArc(
                center: CGPoint(x: rtl, y: rtl),
                radius: rtl,
                startAngle: .degrees(180),
                endAngle: .degrees(270),
                clockwise: false
            )
        }

        path.closeSubpath()
        return path
    }

    private func buildSentTail(path: inout Path, W: CGFloat, bodyH: CGFloat, rbr: CGFloat, rbl: CGFloat) {
        // Partial BR arc — stop at ~4 o'clock so the tail starts further right
        let arcEndX = W - rbr + tailShift
        let arcEndY = bodyH - rbr + sqrt(rbr * rbr - tailShift * tailShift)

        // Arc from right edge to the split point
        let brCenter = CGPoint(x: W - rbr, y: bodyH - rbr)
        let startAngle = Angle.degrees(0)
        let endAngle = Angle(radians: atan2(arcEndY - brCenter.y, arcEndX - brCenter.x))
        path.addArc(center: brCenter, radius: rbr, startAngle: startAngle, endAngle: endAngle, clockwise: false)

        // Tail tip
        let tipX = arcEndX - 1
        let tipY = bodyH + tailH - 1

        // CP1 follows the arc's tangent for smooth organic exit
        let tanLen: CGFloat = 12
        let tanNormX = -sqrt(rbr * rbr - tailShift * tailShift) / rbr
        let tanNormY = tailShift / rbr
        let cp1x = arcEndX + tanNormX * tanLen
        let cp1y = arcEndY + tanNormY * tanLen

        path.addCurve(
            to: CGPoint(x: tipX, y: tipY),
            control1: CGPoint(x: cp1x, y: cp1y),
            control2: CGPoint(x: tipX + 1.5, y: tipY - 2)
        )

        // Pointier tip — shifted 2px left, slightly rounder
        path.addQuadCurve(
            to: CGPoint(x: tipX - 2.8, y: tipY - 0.2),
            control: CGPoint(x: tipX - 1.2, y: tipY + 0.5)
        )

        // Gentle outward arch back up to bottom edge
        let scoopX = max(W - rbr - rbr * 0.15 + 2, rbl)
        let midX = (tipX + scoopX) / 2
        let midY = (tipY + bodyH) / 2

        path.addCurve(
            to: CGPoint(x: scoopX, y: bodyH),
            control1: CGPoint(x: tipX - 3, y: tipY),
            control2: CGPoint(x: midX - 1, y: midY + 1)
        )

        // Bottom edge to BL
        path.addLine(to: CGPoint(x: rbl, y: bodyH))
    }

    private func buildReceivedTail(path: inout Path, W: CGFloat, bodyH: CGFloat, rbr: CGFloat, rbl: CGFloat, rtl: CGFloat) {
        // Normal BR corner
        path.addArc(
            center: CGPoint(x: W - rbr, y: bodyH - rbr),
            radius: rbr,
            startAngle: .degrees(0),
            endAngle: .degrees(90),
            clockwise: false
        )

        // Bottom edge to scoop start
        let scoopStartX = min(rbl + rbl * 0.15, W - rbr)
        path.addLine(to: CGPoint(x: scoopStartX, y: bodyH))

        // Tail tip
        let tipX = rbl - tailShift + 1
        let tipY = bodyH + tailH - 1
        let midX = (scoopStartX + tipX) / 2
        let midY = (bodyH + tipY) / 2

        // Gentle outward arch down to near tip
        path.addCurve(
            to: CGPoint(x: tipX + 1.5, y: tipY - 0.5),
            control1: CGPoint(x: midX + 1, y: midY + 1),
            control2: CGPoint(x: tipX + 3, y: tipY)
        )

        // Pointier tip
        path.addQuadCurve(
            to: CGPoint(x: tipX, y: tipY),
            control: CGPoint(x: tipX + 0.2, y: tipY + 0.3)
        )

        // Left side of tail: curve back up following tangent
        let arcEndX = rbl - tailShift
        let arcEndY = bodyH - rbl + sqrt(rbl * rbl - tailShift * tailShift)
        let tanLen: CGFloat = 12
        let tanNormX = sqrt(rbl * rbl - tailShift * tailShift) / rbl
        let tanNormY = tailShift / rbl
        let cp2x = arcEndX + tanNormX * tanLen
        let cp2y = arcEndY + tanNormY * tanLen

        path.addCurve(
            to: CGPoint(x: arcEndX, y: arcEndY),
            control1: CGPoint(x: tipX - 1.5, y: tipY - 2),
            control2: CGPoint(x: cp2x, y: cp2y)
        )

        // Complete the rest of the BL arc
        let blCenter = CGPoint(x: rbl, y: bodyH - rbl)
        let blEndAngle = Angle(radians: atan2(arcEndY - blCenter.y, arcEndX - blCenter.x))
        path.addArc(center: blCenter, radius: rbl, startAngle: blEndAngle, endAngle: .degrees(180), clockwise: false)

        // Left edge
        if rtl + rbl < bodyH {
            path.addLine(to: CGPoint(x: 0, y: rtl))
        }

        // TL corner
        path.addArc(
            center: CGPoint(x: rtl, y: rtl),
            radius: rtl,
            startAngle: .degrees(180),
            endAngle: .degrees(270),
            clockwise: false
        )
    }
}

// MARK: - Usage example

struct ChatBubbleView: View {
    let text: String
    let isSent: Bool
    let hasTail: Bool
    let isGroupTop: Bool
    let isGroupMiddle: Bool
    let isGroupBottom: Bool

    init(
        _ text: String,
        isSent: Bool,
        hasTail: Bool = false,
        isGroupTop: Bool = false,
        isGroupMiddle: Bool = false,
        isGroupBottom: Bool = false
    ) {
        self.text = text
        self.isSent = isSent
        self.hasTail = hasTail
        self.isGroupTop = isGroupTop
        self.isGroupMiddle = isGroupMiddle
        self.isGroupBottom = isGroupBottom
    }

    var body: some View {
        Text(text)
            .font(.body)
            .foregroundColor(.white)
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, hasTail ? 15 : 8) // extra padding for tail
            .background(
                ChatBubbleShape(
                    isSent: isSent,
                    hasTail: hasTail,
                    isGroupTop: isGroupTop,
                    isGroupMiddle: isGroupMiddle,
                    isGroupBottom: isGroupBottom
                )
                .fill(isSent ? Color.blue : Color(white: 0.17))
            )
    }
}

// MARK: - Preview

#Preview {
    VStack(alignment: .trailing, spacing: 1) {
        ChatBubbleView("I don't have any plans except tonight I'm watching moose", isSent: true, hasTail: true)

        VStack(alignment: .leading, spacing: 1) {
            ChatBubbleView("Morning, ok do you want to come for a hike with me Saturday?", isSent: false, isGroupTop: true)
            ChatBubbleView("And then we can hang out after", isSent: false, hasTail: true, isGroupBottom: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)

        ChatBubbleView("Sure", isSent: true, hasTail: true)

        ChatBubbleView("Cool!", isSent: false, hasTail: true)
            .frame(maxWidth: .infinity, alignment: .leading)

        ChatBubbleView("Would you like to see that Hail Mary movie", isSent: true, hasTail: true)
    }
    .padding()
    .frame(maxWidth: 400)
    .background(Color(white: 0.11))
}
