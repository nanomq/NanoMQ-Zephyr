//
// Zephyr broker entry point — "embedded minimal conf" bootstrap.
//
// Zephyr has no filesystem, so the broker is started with conf_init()
// defaults plus a few field overrides, then broker() is called directly.
// broker_start() (file parsing, daemonize) is deliberately bypassed.
//
// Startup contract with apps/broker.c::broker(conf *):
//   * conf->url non-NULL  → broker listens on it (nmq-tcp:// ...)
//   * conf->ipc_internal  → cmd server over IPC transport; Zephyr build
//                           has NNG_TRANSPORT_IPC=OFF, so it must be false
//   * log_init() must run first: in the normal flow it is called by
//     broker_start_with_conf() right before broker(); broker() itself
//     never touches the nanolib log module
//   * broker() never returns: it parks in for(;;) nng_msleep(3600000)
//
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_core.h>

#include "nng/nng.h"
#include "nng/supplemental/nanolib/conf.h"
#include "nng/supplemental/nanolib/log.h"
#include "mqtt_api.h" // log_init(conf_log *)

// broker() lives in nanomq/nanomq/apps/broker.c and is not declared in
// broker.h (which only exports broker_start/broker_start_with_conf).
extern int broker(conf *nanomq_conf);

static void
dump_iface_ipv4_cb(struct net_if *iface, struct net_if_addr *ifaddr, void *user_data)
{
	char buf[NET_IPV4_ADDR_LEN];

	if (ifaddr->address.family == AF_INET && ifaddr->addr_type == NET_ADDR_MANUAL) {
		printk("net: ipv4 %s\n", net_addr_ntop(
			AF_INET, &ifaddr->address.in_addr, buf, sizeof(buf)));
	}
	ARG_UNUSED(iface);
	ARG_UNUSED(user_data);
}

static void
dump_iface_ipv4(struct net_if *iface)
{
	if (iface != NULL) {
		net_if_ipv4_addr_foreach(iface, dump_iface_ipv4_cb, NULL);
	}
}

static void
list_iface_cb(struct net_if *iface, void *user_data)
{
	const struct device *dev = net_if_get_device(iface);

	printk("net: iface %p dev=%s up=%d\n", (void *) iface,
	       dev == NULL ? "(null)" : dev->name, net_if_is_up(iface));
	if (user_data != NULL) {
		dump_iface_ipv4(iface);
	}
}

static void
dump_ifaces(void)
{
	net_if_foreach(list_iface_cb, (void *) 1);
}

void
main(void)
{
	conf *nmq_conf;

	if ((nmq_conf = nng_zalloc(sizeof(conf))) == NULL) {
		printk("Cannot allocate configuration, quit\n");
		return;
	}

	conf_init(nmq_conf);

	dump_ifaces();

	// Embedded minimal conf: defaults + key overrides.
	nmq_conf->url          = "nmq-tcp://0.0.0.0:1883";
	nmq_conf->ipc_internal = false; // cmd/reload server needs IPC transport
	nmq_conf->daemon       = false;
#ifdef CONFIG_BROKER_LOG_DEBUG
	nmq_conf->log.level    = NNG_LOG_DEBUG;
#endif

#if defined(ENABLE_LOG)
	// Activate the nanolib log backend (console) and apply conf->log.level
	// — broker_start_with_conf() normally does this, but the embedded demo
	// calls broker() directly.
	log_init(&nmq_conf->log);
	log_add_console(NNG_LOG_WARN, NULL);
#endif

	broker(nmq_conf);

	// broker() only returns on (unreachable) test paths.
	printk("NanoMQ broker exited unexpectedly\n");
}
