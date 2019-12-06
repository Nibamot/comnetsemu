#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8

"""
About: Example of using Network Coding (NC) for transport on a multi-hop topology.
"""


import argparse
import csv
import time
from shlex import split
from subprocess import check_output

from common import SYMBOL_SIZE, META_DATA_LEN
from comnetsemu.net import Containernet, VNFManager
from mininet.link import TCLink
from mininet.log import error, info, setLogLevel
from mininet.node import Controller

NET = None


def get_ofport(ifce: str):
    """Get the openflow port based on iterface name

    :param ifce (str): Name of the interface.
    """
    return check_output(split("ovs-vsctl get Interface {} ofport".format(ifce))).decode(
        "utf-8"
    )


def config_ipv6(action: str):
    value = 0
    if action == "disable":
        value = 1
    check_output(split(f"sysctl -w net.ipv6.conf.all.disable_ipv6={value}"))
    check_output(split(f"sysctl -w net.ipv6.conf.default.disable_ipv6={value}"))


def add_ovs_flows(net, switch_num):
    """Add OpenFlow rules for UDP traffic redirection."""

    proto = "udp"

    for i in range(switch_num - 1):
        check_output(split("ovs-ofctl del-flows s{}".format(i + 1)))
        in_port = get_ofport("s{}-h{}".format((i + 1), (i + 1)))
        out_port = get_ofport("s{}-s{}".format((i + 1), (i + 2)))
        check_output(
            split(
                'ovs-ofctl add-flow s{sw} "{proto},in_port={in_port},actions=output={out_port}"'.format(
                    **{
                        "sw": (i + 1),
                        "in_port": in_port,
                        "out_port": out_port,
                        "proto": proto,
                    }
                )
            )
        )

        if i == 0:
            continue

        in_port = get_ofport("s{}-s{}".format((i + 1), i))
        out_port = get_ofport("s{}-h{}".format((i + 1), (i + 1)))
        check_output(
            split(
                'ovs-ofctl add-flow s{sw} "{proto},in_port={in_port},actions=output={out_port}"'.format(
                    **{
                        "sw": (i + 1),
                        "in_port": in_port,
                        "out_port": out_port,
                        "proto": proto,
                    }
                )
            )
        )


def dump_ovs_flows(switch_num):
    """Dump OpenFlow rules of first switch_num switches"""
    for i in range(switch_num):
        ret = check_output(split("ovs-ofctl dump-flows s{}".format(i + 1)))
        info("### Flow table of the switch s{} after adding flows:\n".format(i + 1))
        print(ret.decode("utf-8"))


def disable_cksum_offload(switch_num):
    """Disable RX/TX checksum offloading"""
    for i in range(switch_num):
        ifce = "s%s-h%s" % (i + 1, i + 1)
        check_output(split("ethtool --offload %s rx off tx off" % ifce))


def deploy_coders(mgr, hosts, rec_st_idx, relay_num, action_map):
    """Deploy en-, re- and decoders on the multi-hop topology.

    Since tests run in a non-powerful VM for teaching purpose, the wait time is
    set to 3 seconds.

    :param mgr (VNFManager): Manager instance for all coders.
    :param hosts (list): List of hosts
    :param rec_st_idx (int): The index of the first recoder (start from 0)
    :param relay_num (int): Number of recoders
    :param action_map (list): Action maps of the recoders, can be forward or recode
    """
    recoders = list()

    info(
        "*** Run NC recoder(s) in the middle, on hosts %s...\n"
        % (", ".join([x.name for x in hosts[rec_st_idx : rec_st_idx + relay_num]]))
    )
    for i in range(rec_st_idx, rec_st_idx + relay_num):
        name = "recoder_on_h%d" % (i + 1)
        rec_cli = "h{}-s{} --action {}".format(i + 1, i + 1, action_map[i - 2])
        recoder = mgr.addContainer(
            name,
            hosts[i].name,
            "nc_coder",
            " ".join(("python3 ./recoder.py", rec_cli)),
            wait=3,
            docker_args={},
        )
        recoders.append(recoder)
    time.sleep(relay_num)
    info("*** Run NC decoder on host %s\n" % hosts[-2].name)
    decoder = mgr.addContainer(
        "decoder",
        hosts[-2].name,
        "nc_coder",
        "python3 ./decoder.py h%d-s%d" % (len(hosts) - 1, len(hosts) - 1),
        wait=3,
        docker_args={},
    )
    info("*** Run NC encoder on host %s\n" % hosts[1].name)
    encoder = mgr.addContainer(
        "encoder",
        hosts[1].name,
        "nc_coder",
        "python3 ./encoder.py h2-s2",
        wait=3,
        docker_args={},
    )

    return (encoder, decoder, recoders)


def remove_coders(mgr, coders):
    encoder, decoder, recoders = coders
    mgr.removeContainer(encoder.name)
    mgr.removeContainer(decoder.name)
    for r in recoders:
        mgr.removeContainer(r.name)


def print_coders_log(coders, coder_log_conf):
    """Print the logs of coders based on values in coder_log_conf."""
    encoder, decoder, recoders = coders
    if coder_log_conf.get("recoder", None):
        info("*** Log of recoders: \n")
        for r in recoders:
            print(r.getLogs())

    if coder_log_conf.get("decoder", None):
        info("*** Log of decoder: \n")
        print(decoder.dins.logs().decode("utf-8"))

    if coder_log_conf.get("encoder", None):
        info("*** Log of the encoder: \n")
        print(encoder.dins.logs().decode("utf-8"))


def run_iperf_test(h_clt, h_srv, proto, time=10, print_clt_log=False):
    """Run Iperf tests between h_clt and h_srv (DockerHost) and print the output
    of the Iperf server.

    :param proto (str):  Transport protocol, UDP or TCP
    :param time (int): Duration of the traffic flow
    :param print_clt_log (Bool): If true, print the log of the Iperf client
    """
    info(
        "Run Iperf test between {} (Client) and {} (Server), protocol: {}\n".format(
            h_clt.name, h_srv.name, proto
        )
    )
    iperf_client_para = {
        "server_ip": h_srv.IP(),
        "port": 9999,
        "bw": "50K",
        "time": time,
        "interval": 1,
        "length": str(SYMBOL_SIZE - META_DATA_LEN),
        "proto": "-u",
        "suffix": "> /dev/null 2>&1 &",
    }
    if proto == "UDP" or proto == "udp":
        iperf_client_para["proto"] = "-u"
        iperf_client_para["suffix"] = ""

    h_srv.cmd(
        "iperf -s -p 9999 -i 1 {} > /tmp/iperf_server.log 2>&1 &".format(
            iperf_client_para["proto"]
        )
    )

    iperf_clt_cmd = """iperf -c {server_ip} -p {port} -t {time} -i {interval} -b {bw} -l {length} {proto} {suffix}""".format(
        **iperf_client_para
    )
    print("Iperf client command: {}".format(iperf_clt_cmd))
    ret = h_clt.cmd(iperf_clt_cmd)

    info("*** Output of Iperf server:\n")
    print(h_srv.cmd("cat /tmp/iperf_server.log"))

    if print_clt_log:
        info("*** Output of Iperf client:\n")
        print(ret)


def create_topology(net, host_num):
    """Create the multi-hop topology

    :param net (Mininet):
    :param host_num (int): Number of hosts
    """

    hosts = list()

    if host_num < 5:
        raise RuntimeError("Require at least 5 hosts")
    try:
        info("*** Adding controller\n")
        net.addController("c0")

        info("*** Adding Docker hosts and switches in a multi-hop chain topo\n")
        last_sw = None
        # Connect hosts
        for i in range(host_num):
            # Let kernel schedule all hosts based on their workload.
            # The recoder needs more computational resources than en- and decoder.
            # Hard-coded cfs quota can cause different results on machines with
            # different performance.
            host = net.addDockerHost(
                "h%s" % (i + 1),
                dimage="dev_test",
                ip="10.0.0.%s" % (i + 1),
                docker_args={"hostname": "h%s" % (i + 1)},
            )
            hosts.append(host)
            switch = net.addSwitch("s%s" % (i + 1))
            # No losses between each host-switch pair, this link is used to
            # transmit OAM packet.
            # Losses are emulated with links between switches.
            net.addLinkNamedIfce(switch, host, delay="20ms")
            if last_sw:
                net.addLinkNamedIfce(switch, last_sw, delay="20ms", loss=20)
            last_sw = switch

        return hosts

    except Exception as e:
        error(e)
        net.stop()


def run_multihop_nc_test(host_num, profile, coder_log_conf):

    config_ipv6(action="disable")
    net = Containernet(controller=Controller, link=TCLink, autoStaticArp=True)
    NET = net
    mgr = VNFManager(net)
    hosts = create_topology(net, host_num)
    # Number of relays in the middle.
    relay_num = host_num - 2 - 2
    rec_st_idx = 2

    try:
        info("*** Starting network\n")
        net.start()
        info("*** Adding OpenFlow rules\n")
        add_ovs_flows(net, host_num)
        info("*** Disable Checksum offloading\n")
        disable_cksum_offload(host_num)

        if profile == PROFILES["mobile_recoder_deterministic"]:
            info("*** Run mobile recoder experiment.\n")
            for i in range(relay_num):
                action_map = ["forward"] * relay_num
                if not ALL_FOWARD:
                    action_map[i] = "recode"
                info(
                    "Number of recoders: %s, the action map: %s\n"
                    % (relay_num, ", ".join(action_map))
                )
                coders = deploy_coders(mgr, hosts, rec_st_idx, relay_num, action_map)
                # Wait for coders to be ready
                time.sleep(3)
                run_iperf_test(hosts[0], hosts[-1], "udp", 30)
                print_coders_log(coders, coder_log_conf)
                remove_coders(mgr, coders)

                if ALL_FOWARD:
                    break

        info("*** Emulation stops...\n")

    except Exception as e:
        error("*** Emulation has errors:\n")
        error(e)
    finally:
        info("*** Stopping network\n")
        net.stop()
        mgr.stop()
        config_ipv6(action="enable")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Example of using Network Coding (NC) for transport on a multi-hop topology."
    )
    parser.add_argument(
        "--all_forward",
        action="store_true",
        help="All relays perform only store and forward."
        "Used to test encoder and decoder.",
    )

    # Default global parameters
    ALL_FOWARD = False
    HOST_NUM = 7

    args = parser.parse_args()
    ALL_FOWARD = args.all_forward

    setLogLevel("info")
    coder_log_conf = {"encoder": False, "decoder": False, "recoder": False}

    PROFILES = {"mobile_recoder_deterministic": 0}
    run_multihop_nc_test(
        HOST_NUM, PROFILES["mobile_recoder_deterministic"], coder_log_conf
    )
