// What Chrome hands back, and what the reader makes of it.
//
// The bugs this suite exists for all returned success and looked like an
// ordinary "no page in front", which is the same thing a closed browser looks
// like. None could have been caught by reading the code: one was a word that
// means something else inside a tell block, one a trim that ate the delimiter
// it was about to split on, and one an Apple Event addressed at a name when
// two processes answered to it. The first two are gone with the text protocol
// they lived in -- there is no reply to mis-split any more. What is left is
// the field contract, which is asserted here, and the addressing, which is a
// pid parameter and so is checked by the compiler.
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
        // The ordinary page.
        let page = chromeTabFields(title: "Home - Workday",
                                   url: "https://wd5.myworkday.com/rubrik/d/home.htmld")
        check("a page is read", page?.title == "Home - Workday")
        check("its address is read",
              page?.url == "https://wd5.myworkday.com/rubrik/d/home.htmld")

        // Workday's login page has no title. An untitled page is still a page,
        // and discarding it throws away a perfectly good address.
        let untitled = chromeTabFields(
            title: "",
            url: "https://wd5.myworkday.com/wday/authgwy/rubrik/login.htmld")
        check("an untitled page still has an address",
              untitled?.url == "https://wd5.myworkday.com/wday/authgwy/rubrik/login.htmld")
        check("an untitled page has an empty title", untitled?.title == "")

        // Chrome returns nothing at all for a window with no tab in it, and
        // nil is not a page.
        check("a missing title is still a page",
              chromeTabFields(title: nil, url: "https://example.com/")?.url
                  == "https://example.com/")

        // An address is the one half that has to be there. A page cannot be
        // classified without it, so anything missing one is no page.
        check("no address is refused",
              chromeTabFields(title: "Some Title", url: nil) == nil)
        check("a blank address is refused",
              chromeTabFields(title: "Some Title", url: "   ") == nil)
        check("an empty address is refused",
              chromeTabFields(title: "Some Title", url: "") == nil)
        check("nothing at all is refused",
              chromeTabFields(title: nil, url: nil) == nil)

        // Titles are data. A title made of punctuation, or one carrying the
        // tab character the old text protocol used as its delimiter, is a
        // title -- there is nothing left for it to break.
        let punctuation = chromeTabFields(title: "| - | ? & # |",
                                          url: "https://example.com/")
        check("punctuation in a title survives", punctuation?.title == "| - | ? & # |")
        let tabbed = chromeTabFields(title: "a\tb", url: "https://example.com/")
        check("a tab character in a title survives", tabbed?.title == "a\tb")
        check("a tab character does not eat the address",
              tabbed?.url == "https://example.com/")

        // Surrounding whitespace is Chrome's, not the page's.
        let padded = chromeTabFields(title: "  Spaced  ",
                                     url: "  https://example.com/  ")
        check("the title is trimmed", padded?.title == "Spaced")
        check("the address is trimmed", padded?.url == "https://example.com/")

        if failures == 0 { print("chrome tab tests passed") }
        exit(failures == 0 ? 0 : 1)
    }
}
