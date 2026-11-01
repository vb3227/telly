# Security and data handling

## Credentials

Copy the public board template into `include/board_config.h`, which is ignored. API keys included in firmware can be extracted from a device; treat flashed boards and binaries as credential-bearing assets. Board commands and gateway requests require separate matching tokens. Never paste credentials into issue reports or commit them.

## Trust boundaries

Recognized speech, model outputs and scene text are untrusted input. Only supported, bounded JSON operations are executable. Program validation runs on both host and firmware. The planning worker cannot approve its own tools or flash hardware. Local Hi Telly commands invalidate stale cloud actions. Media sessions are bounded and do not invoke models.

HTTPS verifies server certificates. The outbound relay avoids exposing the board on a public inbound port, but it is not a VPN. The host relay is part of the trusted control plane. Do not disable authentication or certificate verification to troubleshoot connectivity.

## Data retention

`.telly/` contains local control tokens, model sessions, transcripts, captured JPEGs, results and historical diagnostic archives. It must never be committed or included in a source release. Captured audio goes to Groq; routing prompts include skill definitions; complex requests go to the configured Codex service. Camera files stay on the laptop. Automatic cleanup of runtime media is not implemented.

## Reporting

Do not put credentials or private media in public issues. Contact the repository owner through a private channel agreed with them. No dedicated security contact or supported-release policy has been established for this prototype.

The publication checker catches known local credentials, common key formats, workstation paths and accidental private-network addresses in the explicit release set. It is a practical preflight, not a comprehensive secret scanner or security audit. Review the Git diff and run an independent scanner before making a repository public.
