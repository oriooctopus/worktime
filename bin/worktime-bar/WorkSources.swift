import AppKit

// MARK: - Local pages the classifier cannot judge on its own

// A page on localhost or on disk says nothing about whose it is: the port or
// the directory is the only evidence, and which of them are the job is a fact
// about this person. So the first time Chrome is in front of one nobody has
// ruled on, the bar asks, and the answer is written to the profile the probe
// and the exporter already read -- `work_localhost_ports` and
// `work_file_prefixes` for a Yes, `dismissed_local_sources` for a No, so a
// declined source is never asked about twice.

let DEFAULT_WORK_PORTS = [3000, 3001, 3002, 3003, 3004, 3005]

struct LocalSource: Equatable {
    enum Kind { case port, directory }
    let kind: Kind
    /// The port number, or the directory as a `file://` URL ending in "/".
    let value: String

    var label: String { kind == .port ? "localhost:\(value)" : value }
    /// What is remembered in `dismissed_local_sources`.
    var dismissKey: String { (kind == .port ? "port:" : "dir:") + value.lowercased() }
}

private let localhostPattern = try! NSRegularExpression(
    pattern: "^https?://(?:localhost|127\\.0\\.0\\.1|\\[::1\\]):(\\d{1,5})(?:[/?#]|$)",
    options: .caseInsensitive)

/// The localhost port or local directory a page belongs to, or nil for any
/// other address.
func localSource(url: String) -> LocalSource? {
    let range = NSRange(url.startIndex..., in: url)
    if let m = localhostPattern.firstMatch(in: url, range: range),
       let r = Range(m.range(at: 1), in: url) {
        return LocalSource(kind: .port, value: String(url[r]))
    }
    if url.lowercased().hasPrefix("file://"), let u = URL(string: url) {
        var dir = u.deletingLastPathComponent().absoluteString
        if !dir.hasSuffix("/") { dir += "/" }
        return LocalSource(kind: .directory, value: dir)
    }
    return nil
}

/// Reads and writes the profile's local-source keys. Re-read on every call:
/// the file is tiny, only consulted when Chrome is on a local page, and a
/// hand edit should take effect without restarting the bar.
struct WorkSourceStore {
    let path: String

    static var profilePath: String {
        NSString(string: "~/.config/worktime/profile.json").expandingTildeInPath
    }

    private func load() -> [String: Any] {
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { fatalError("profile \(path) is missing or not a JSON object") }
        return obj
    }

    private func save(_ profile: [String: Any]) {
        let data = try! JSONSerialization.data(
            withJSONObject: profile, options: [.prettyPrinted, .sortedKeys])
        try! data.write(to: URL(fileURLWithPath: path), options: .atomic)
    }

    private func ports(_ p: [String: Any]) -> [Int] {
        (p["work_localhost_ports"] as? [Int]) ?? DEFAULT_WORK_PORTS
    }

    private func strings(_ p: [String: Any], _ key: String) -> [String] {
        (p[key] as? [String]) ?? []
    }

    /// True once somebody has ruled on `source`, either way.
    func isKnown(_ source: LocalSource) -> Bool {
        let p = load()
        if strings(p, "dismissed_local_sources").contains(source.dismissKey) { return true }
        switch source.kind {
        case .port:
            return ports(p).contains(Int(source.value)!)
        case .directory:
            let dir = source.value.lowercased()
            return strings(p, "work_file_prefixes").contains {
                dir.hasPrefix($0.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/")
            }
        }
    }

    func track(_ source: LocalSource) {
        var p = load()
        switch source.kind {
        case .port:
            p["work_localhost_ports"] = ports(p) + [Int(source.value)!]
        case .directory:
            p["work_file_prefixes"] = strings(p, "work_file_prefixes") + [source.value]
        }
        save(p)
    }

    func dismiss(_ source: LocalSource) {
        var p = load()
        p["dismissed_local_sources"] = strings(p, "dismissed_local_sources") + [source.dismissKey]
        save(p)
    }
}

/// Asks about each unruled local source, one panel at a time.
final class WorkSourcePrompter {
    private let store: WorkSourceStore
    private let present: Bool
    private var panel: WorkSourcePanel?
    private var inFlight: String?

    init(store: WorkSourceStore = WorkSourceStore(path: WorkSourceStore.profilePath),
         present: Bool = true) {
        self.store = store
        self.present = present
    }

    /// Call with every Chrome address sampled; safe from any thread.
    func consider(url: String) {
        guard let source = localSource(url: url) else { return }
        DispatchQueue.main.async {
            // The same source keeps being sampled every few seconds while its
            // panel is up, and a second question about it would be noise.
            guard self.inFlight == nil, !self.store.isKnown(source) else { return }
            self.inFlight = source.dismissKey
            self.panel = WorkSourcePanel(
                source: source, present: self.present,
                onYes: { [weak self] in self?.answered { $0.track(source) } },
                onNo: { [weak self] in self?.answered { $0.dismiss(source) } })
        }
    }

    private func answered(_ write: (WorkSourceStore) -> Void) {
        write(store)
        panel = nil
        inFlight = nil
    }
}
