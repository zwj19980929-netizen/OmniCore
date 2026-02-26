"""
CNVD 最新漏洞爬取脚本
爬取国家信息安全漏洞共享平台的最新漏洞信息
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright


async def scrape_cnvd_vulnerabilities(limit: int = 10):
    """
    爬取 CNVD 最新漏洞列表

    Args:
        limit: 爬取数量
    """
    print("=" * 70)
    print(f"CNVD 最新漏洞爬取 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    async with async_playwright() as p:
        print("\n[1/4] 启动浏览器...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print("[2/4] 访问 CNVD 漏洞列表页面...")
        try:
            await page.goto(
                "https://www.cnvd.org.cn/flaw/list",
                wait_until="domcontentloaded",
                timeout=30000
            )
            print(f"      页面标题: {await page.title()}")
        except Exception as e:
            print(f"      访问失败，尝试备用方式: {e}")
            await page.goto(
                "https://www.cnvd.org.cn/",
                wait_until="domcontentloaded",
                timeout=30000
            )

        # 等待页面加载
        await page.wait_for_timeout(3000)

        print("[3/4] 提取漏洞数据...")
        vulnerabilities = []

        # 尝试多种选择器
        # CNVD 漏洞列表通常在表格中
        rows = await page.query_selector_all("table tbody tr")

        if not rows:
            # 尝试其他选择器
            rows = await page.query_selector_all(".list_list tbody tr")

        if not rows:
            rows = await page.query_selector_all("tr[onclick]")

        if not rows:
            # 打印页面内容帮助调试
            content = await page.content()
            print(f"      未找到表格行，页面长度: {len(content)}")
            # 尝试获取任何链接
            links = await page.query_selector_all("a[href*='CNVD']")
            print(f"      找到 {len(links)} 个 CNVD 相关链接")

            for i, link in enumerate(links[:limit]):
                try:
                    text = await link.inner_text()
                    href = await link.get_attribute("href")
                    if text.strip() and "CNVD-" in (href or ""):
                        vulnerabilities.append({
                            "index": i + 1,
                            "title": text.strip()[:80],
                            "link": href if href.startswith("http") else f"https://www.cnvd.org.cn{href}",
                        })
                except:
                    continue
        else:
            print(f"      找到 {len(rows)} 行数据")

            for i, row in enumerate(rows[:limit]):
                try:
                    # 获取所有单元格
                    cells = await row.query_selector_all("td")

                    if len(cells) >= 2:
                        # 尝试获取漏洞编号和标题
                        vuln_id = ""
                        title = ""
                        link = ""
                        date = ""
                        severity = ""

                        # 查找链接
                        link_elem = await row.query_selector("a")
                        if link_elem:
                            title = await link_elem.inner_text()
                            href = await link_elem.get_attribute("href")
                            link = href if href and href.startswith("http") else f"https://www.cnvd.org.cn{href}" if href else ""

                        # 尝试从各个单元格提取信息
                        for j, cell in enumerate(cells):
                            text = (await cell.inner_text()).strip()
                            if "CNVD-" in text:
                                vuln_id = text
                            elif "2026" in text or "2025" in text:
                                date = text
                            elif text in ["高", "中", "低", "高危", "中危", "低危"]:
                                severity = text

                        if title or vuln_id:
                            vulnerabilities.append({
                                "index": i + 1,
                                "vuln_id": vuln_id,
                                "title": title[:80] if title else vuln_id,
                                "severity": severity,
                                "date": date,
                                "link": link,
                            })
                except Exception as e:
                    print(f"      提取第 {i+1} 行失败: {e}")
                    continue

        print("[4/4] 关闭浏览器...")
        await browser.close()

        # 输出结果
        print("\n" + "=" * 70)
        print(f"爬取结果 - 共 {len(vulnerabilities)} 条漏洞")
        print("=" * 70)

        if vulnerabilities:
            for vuln in vulnerabilities:
                print(f"\n{vuln.get('index', '')}. {vuln.get('title', 'N/A')}")
                if vuln.get('vuln_id'):
                    print(f"   编号: {vuln['vuln_id']}")
                if vuln.get('severity'):
                    print(f"   危害等级: {vuln['severity']}")
                if vuln.get('date'):
                    print(f"   日期: {vuln['date']}")
                if vuln.get('link'):
                    print(f"   链接: {vuln['link']}")
        else:
            print("\n未能获取到漏洞数据，可能需要调整选择器或网站结构已变化")

        print("\n" + "=" * 70)

        return vulnerabilities


async def main():
    """主函数"""
    print("""
    ╔═══════════════════════════════════════════════════════════════╗
    ║           CNVD 国家信息安全漏洞共享平台 爬虫                  ║
    ║                    OmniCore 测试脚本                          ║
    ╚═══════════════════════════════════════════════════════════════╝
    """)

    vulns = await scrape_cnvd_vulnerabilities(limit=10)

    # 保存到文件
    if vulns:
        from pathlib import Path
        output_path = Path.home() / "Desktop" / "cnvd_vulnerabilities.txt"

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"CNVD 最新漏洞列表\n")
            f.write(f"爬取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n\n")

            for vuln in vulns:
                f.write(f"{vuln.get('index', '')}. {vuln.get('title', 'N/A')}\n")
                if vuln.get('vuln_id'):
                    f.write(f"   编号: {vuln['vuln_id']}\n")
                if vuln.get('severity'):
                    f.write(f"   危害等级: {vuln['severity']}\n")
                if vuln.get('date'):
                    f.write(f"   日期: {vuln['date']}\n")
                if vuln.get('link'):
                    f.write(f"   链接: {vuln['link']}\n")
                f.write("\n")

        print(f"\n✅ 结果已保存到: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
