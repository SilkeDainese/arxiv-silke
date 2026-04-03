import jinja2

# Setup a minimal Jinja2 environment for testing
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader("setup/templates"),
    autoescape=True
)

def test_ssti_prevention():
    template = env.get_template("error.html")

    # Malicious inputs
    malicious_message = "{{ 7*7 }}"
    malicious_title = "<script>alert(1)</script>"
    malicious_tip = "{% if True %}Exploit{% endif %}"

    rendered = template.render(
        code=500,
        title=malicious_title,
        message=malicious_message,
        tips=[malicious_tip]
    )

    # Check that SSTI payloads are NOT executed
    # They should be rendered literally (and escaped if they contain HTML special chars)

    # Check for {{ 7*7 }}
    if "49" in rendered:
        print("FAIL: 49 in rendered (SSTI EXPLOITED via {{ 7*7 }})")
        return
    if "{{ 7*7 }}" not in rendered:
        print("FAIL: {{ 7*7 }} not in rendered")
        return

    # Check for {% if True %}Exploit{% endif %}
    # If executed, it would just be "Exploit"
    # If not executed, it's the whole string.
    if "{% if True %}Exploit{% endif %}" not in rendered:
        # Check if it's escaped (it shouldn't be if no < > & " ')
        print("FAIL: SSTI payload not found literally")
        return

    # Verify it wasn't executed: if executed, it wouldn't contain the tags.
    # Actually, if we see the tags, it's definitely not executed.

    # Check that XSS payloads are escaped
    if "&lt;script&gt;alert(1)&lt;/script&gt;" not in rendered:
        print("FAIL: XSS payload not escaped")
        return
    if "<script>alert(1)</script>" in rendered:
        print("FAIL: <script>alert(1)</script> in rendered (XSS EXPLOITED)")
        return

    print("Security verification PASSED: Both SSTI and XSS are prevented by render_template and autoescape.")

if __name__ == "__main__":
    test_ssti_prevention()
