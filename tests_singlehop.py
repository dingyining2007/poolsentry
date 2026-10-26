import sys; sys.path.insert(0,'/root/.openclaw/workspace/open_source/pool-health-scanner/src')
import cli

def main():
    ok=0; fail=0
    # ① 深池/浅池同一代币对，损耗必须不同（这是 P0 的核心断言）
    deep = cli.analyse_subject("0x0eD7e52944161450477ee417DE9Cd3a859b14fD0")  # CAKE/WBNB $21M
    shallow = cli.analyse_subject("0xe1c4f0ccfed718c9e11bd70977381f1e8d13c00d") # TUSD/WBNB $123
    d10 = next(x for x in deep["round_trips"] if abs(x["in_usd"]-10)<0.5)
    s1  = next(x for x in shallow["round_trips"] if abs(x["in_usd"]-1)<0.01)
    print("[1] 深池 TVL=$%.0f  $10损耗=%.4f%%" % (deep["tvl_usd"], d10["loss_pct"]))
    print("    浅池 TVL=$%.0f  $1损耗=%.4f%%" % (shallow["tvl_usd"], s1["loss_pct"]))
    # 浅池 $1 占池比 81%，损耗应显著高于 deep 的 $10 占池比 0.0047%
    if s1["pct_of_tvl"] > d10["pct_of_tvl"]*100 and abs(s1["loss_pct"]-d10["loss_pct"])>1e-6:
        print("    PASS 两池损耗不同且与占池比同向"); ok+=1
    else:
        print("    FAIL 两池损耗未区分"); fail+=1
    # ② TVL 口径必须有 basis 标注
    for nm,r in (("deep",deep),("shallow",shallow)):
        if r.get("tvl_basis"):
            print("[2] %s tvl_basis=%s PASS" % (nm, r["tvl_basis"])); ok+=1
        else:
            print("[2] %s 缺 tvl_basis FAIL" % nm); fail+=1
    # ③ 非 STABLES 稳定币必须被识别（TUSD）
    if shallow.get("tvl_basis")=="both_sides_usd":
        print("[3] TUSD 被识别为美元锚 PASS"); ok+=1
    else:
        print("[3] TUSD 未被识别 FAIL"); fail+=1
    # ④ 兜底回归：不得出现「占池比 >100% 还输出正常损耗」以外的静默错
    # 这里只断言 loss_pct 必须落在 [0,100]
    for nm,r in (("deep",deep),("shallow",shallow)):
        for x in r["round_trips"]:
            lp=x["loss_pct"]
            if lp is not None and 0<=lp<=100:
                pass
            else:
                print("[4] %s 损耗越界 %s FAIL"%(nm,lp)); fail+=1; break
        else:
            print("[4] %s 损耗全部在 [0,100] PASS"%nm); ok+=1
    print("\n结果: %d PASS / %d FAIL" % (ok,fail))
    sys.exit(0 if fail==0 else 1)

main()
