 
from bs4 import BeautifulSoup
import re
from core.log import logger

def format_content(content:str,content_format:str='html'):
    #格式化内容
    # content_format: 'text' or 'markdown' or 'html'
    # content: str
    # return: str
    # 防御性检查：确保 content 不是 None
    if not content:
        return ""
    try:
        if content_format == 'clean':
            soup = BeautifulSoup(content, 'html.parser')
            for tag in soup.find_all(['script', 'style', 'iframe', 'form', 'noscript']):
                tag.decompose()

            # 微信图片经常使用懒加载属性，真实地址不一定放在 src 中。
            # 先统一恢复到 src，再清理非语义属性，避免清爽版丢图或丢失图片地址。
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if not src or src.startswith('data:'):
                    for attr in ('data-src', 'data-original', 'data-lazy-src', 'data-backup-src'):
                        candidate = img.get(attr)
                        if candidate:
                            img['src'] = candidate
                            break
                if img.get('title') and not img.get('alt'):
                    img['alt'] = img['title']

            allowed_attrs = {
                'a': {'href', 'title'},
                'img': {'src', 'alt', 'title'},
            }
            for tag in soup.find_all(True):
                attrs = allowed_attrs.get(tag.name, set())
                tag.attrs = {key: value for key, value in tag.attrs.items() if key in attrs}
            for tag in soup.find_all(['span', 'font']):
                tag.unwrap()
            content = str(soup)
            content = re.sub(r'\n\s*\n\s*\n+', '\n\n', content)
        elif content_format == 'text':
            # 去除HTML标签，保留纯文本
            soup = BeautifulSoup(content, 'html.parser')
            text = soup.get_text().strip()
            content = re.sub(r'\n\s*\n', '\n', text)
        elif content_format == 'markdown':
            # 去除span和font标签，只保留内容
            soup = BeautifulSoup(content, 'html.parser')
            for tag in soup.find_all(['span', 'font','div','strong','b']):
                tag.unwrap()
            for tag in soup.find_all(True):
                if 'style' in tag.attrs:
                  del tag.attrs['style']
                if 'class' in tag.attrs:
                  del tag.attrs['class']
                if 'data-pm-slice' in tag.attrs:
                  del tag.attrs['data-pm-slice']
                if 'data-title' in tag.attrs:
                  # tag.append(tag.attrs['data-title'])
                  del tag.attrs['data-title']
            
                    
            content = str(soup)
            # 替换 p 标签中的换行符为空
            content = re.sub(r'(<p[^>]*>)([\s\S]*?)(<\/p>)', lambda m: m.group(1) + re.sub(r'\n', '', m.group(2)) + m.group(3), content)
            content = re.sub(r'\n\s*\n\s*\n+', '\n', content)
            content = re.sub(r'\*', '', content)
            # print(content)
            from markdownify import markdownify as md
            # 处理图片标签，保留title属性
            soup = BeautifulSoup(content, 'html.parser')
            for img in soup.find_all('img'):
                if 'title' in img.attrs:
                    img['alt'] = img['title']
            content = str(soup)
            # 转换HTML到Markdown
            content = md(content, heading_style="ATX", bullets='-*+', code_language='python')
            content = re.sub(r'\n\s*\n\s*\n+', '\n\n', content)
            
    except Exception as e:
        logger.error('format_content error: %s',e)
    return content
