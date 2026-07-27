<?php
// `true` tells AdminNeo the user is already authenticated by an external
// service. Here that "external service" is the fact that the port is bound to
// 127.0.0.1 and only started on demand behind the `tools` profile.
//
// Anyone who can reach this port gets full database access with no prompt. That
// is the point, and it is why the port is never exposed beyond loopback.

return [
    new \AdminNeo\ExternalLoginPlugin(true),
];
