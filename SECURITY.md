# Security policy

NexusWindows for Claude drives real Windows applications, so safety issues matter more than usual: a way to make the
agent pay, send, publish, delete permanently or confirm without `confirm: true`, to read or drive a password manager,
a credential prompt or the secure desktop, to type into a password field, to take the user's mouse, keyboard or
foreground without `foreground=true`, or to act during the emergency stop.

Please report such issues privately through
[GitHub private vulnerability reporting](https://github.com/pironjulien/NexusWindows-Claude/security/advisories/new),
not in a public issue. Include the tool call, the application involved and what happened. You will get an answer
within a few days.

Only the latest release is supported.

## Disclosure process and timeline

- A vulnerability report gets an acknowledgement within 3 days.
- Within 14 days you get an assessment and a fix or mitigation plan; a critical issue (one of the cases above) gets a
  fixed release within 30 days.
- Disclosure is coordinated: the fix ships with a GitHub security advisory (and a CVE when it applies) crediting the
  reporter who wishes it. Please keep the report private until then, or for 90 days at most.
