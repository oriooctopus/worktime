// What comes back from Chrome, and what the parse makes of it.
//
// Both bugs this suite exists for returned exit status 0 and looked like an
// ordinary "no page in front", which is the same thing a closed browser looks
// like. Neither could have been caught by reading the code -- the first was a
// word that means something else inside a tell block, the second a trim that
// ate the delimiter it was about to split on. What separates them from a real
// empty answer is only the bytes, so the bytes are what this asserts on.
import Foundation

@main
enum ChromeTabTests {
    static var failures = 0

    static func check(_ label: String, _ ok: Bool) {
        if !ok {
            failures += 1
            print("FAIL: \(label)")
        }
    }

    static func main() {
        // The ordinary reply.
        let page = parseChromeTabReply("Home - Workday\thttps://wd5.myworkday.com/rubrik/d/home.htmld\n")
        check("a page is read", page?.title == "Home - Workday")
        check("its address is read",
              page?.url == "https://wd5.myworkday.com/rubrik/d/home.htmld")

        // The bug: Workday's login page has no title, so the reply begins
        // with the delimiter. Trimming before splitting ate it, left one
        // component, and threw the address away with it.
        let untitled = parseChromeTabReply("\thttps://wd5.myworkday.com/wday/authgwy/rubrik/login.htmld\n")
        check("an untitled page still has an address",
              untitled?.url == "https://wd5.myworkday.com/wday/authgwy/rubrik/login.htmld")
        check("an untitled page has an empty title", untitled?.title == "")

        // The earlier bug, as its literal output: `tab` inside the tell block
        // came back as the word, so there was nothing to split on.
        check("a reply with no delimiter is refused",
              parseChromeTabReply("Inbox - Gmailtabhttps://mail.google.com/\n") == nil)

        // An address is the one half that has to be there. A page cannot be
        // classified without it, so a reply carrying only a title is no page.
        check("a reply with no address is refused",
              parseChromeTabReply("Some Title\t\n") == nil)
        check("an empty reply is refused", parseChromeTabReply("\n") == nil)
        check("a blank address is refused",
              parseChromeTabReply("Some Title\t   \n") == nil)

        // A title made of punctuation is why the delimiter is a tab and not
        // any of the characters a page might put in its own name.
        let punctuation = parseChromeTabReply("| - | ? & # |\thttps://example.com/\n")
        check("punctuation in a title survives", punctuation?.title == "| - | ? & # |")
        check("punctuation does not eat the address",
              punctuation?.url == "https://example.com/")

        // Three fields means something put a tab in a title. Guessing which
        // one is the address would be a coin flip, so it is not a page.
        check("more than two fields is refused",
              parseChromeTabReply("a\tb\thttps://example.com/\n") == nil)

        // The script itself: the delimiter must not be named inside the tell
        // block, whoever edits it next.
        for arg in CHROME_TAB_SCRIPT where arg.contains("tell application") {
            check("the delimiter is not `tab` inside the tell block",
                  !arg.contains("& tab &"))
        }

        if failures == 0 { print("chrome tab tests passed") }
        exit(failures == 0 ? 0 : 1)
    }
}
