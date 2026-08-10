# Chess controller reverse proxy
server {
    listen %ip%:%proxy_port%;
    server_name %domain_idn% %alias_idn%;
    error_log /var/log/%web_system%/domains/%domain%.error.log error;
    include %home%/%user%/conf/web/%domain%/nginx.forcessl.conf*;
    include %home%/%user%/conf/web/%domain%/nginx.conf_*;
    location / {
        proxy_pass http://127.0.0.1:8767;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 25m;
        proxy_read_timeout 600s;
    }
}
