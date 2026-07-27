<?php
// AdminNeo configuration. Exactly one server is defined, which is what makes
// ExternalLoginPlugin log in automatically (see adminneo-plugins.php).
//
// The password is read from the mounted Docker secret at request time, so it is
// never copied into a file on disk and stays in sync if it is ever rotated.

return [
    "servers" => [
        // The array key identifies the entry; `server` is the address the plugin
        // actually dials (getCredentials() reads it). Omitting it means an empty
        // host and a silent 403 back to the login page.
        "db" => [
            "driver" => "mysql",
            "server" => "db",
            "name" => getenv("SITE_NAME") ?: "WordPress",
            "database" => getenv("MYSQL_DATABASE") ?: "wordpress",
            "username" => getenv("MYSQL_USER") ?: "wordpress",
            "password" => trim((string) @file_get_contents("/run/secrets/mysql_password")),
        ],
    ],
];
